# app.py
import os
import json
import logging
import base64
import time
import uuid
import stripe
from io import BytesIO
from flask import Flask, Response, request, stream_with_context, session, jsonify, redirect, url_for, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
from PIL import Image
from markupsafe import escape # Import for secure HTML rendering

# --- 1. Initial Configuration ---
# Load environment variables from a .env file.
# This is crucial for security and should be your only source for secret keys.
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Application Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-and-long-random-key-for-myth-ai-v6-safe')
DATABASE_FILE = 'database.json'
# Path to your new static HTML file
HTML_FILE_PATH = os.path.join(os.path.dirname(__file__), 'index.html')

# --- Site & API Configuration ---
SITE_CONFIG = {
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    "STRIPE_SECRET_KEY": os.environ.get('STRIPE_SECRET_KEY'),
    "STRIPE_PUBLIC_KEY": os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_51Ru4xPBSm9qhr9Ev02LOLySoFIztGhmrgUebvTUJtaRO9TFVJE0GwXSlNe3Nd489WpxmrQNIzIoRAxfuhtE0f24o00e6WUfhCb'),
    "STRIPE_WEBHOOK_SECRET": os.environ.get('STRIPE_WEBHOOK_SECRET'), # ADD THIS to your .env file
    "STRIPE_PRO_PRICE_ID": os.environ.get('STRIPE_PRO_PRICE_ID', 'YOUR_PRO_PRICE_ID_HERE'),
    "STRIPE_ULTRA_PRICE_ID": os.environ.get('STRIPE_ULTRA_PRICE_ID', 'YOUR_ULTRA_PRICE_ID_HERE'),
    "STRIPE_PLUS_PRICE_ID": os.environ.get('STRIPE_PLUS_PRICE_ID', 'YOUR_PLUS_PRICE_ID_HERE'),
    "YOUR_DOMAIN": os.environ.get('YOUR_DOMAIN', 'http://localhost:5000'),
    "SECRET_REGISTRATION_KEY": os.environ.get('SECRET_REGISTRATION_KEY', 'SUPER_SECRET_KEY_123')
}


# --- API Initialization ---
GEMINI_API_CONFIGURED = False
try:
    if not SITE_CONFIG["GEMINI_API_KEY"]:
        logging.critical("GEMINI_API_KEY environment variable not set.")
    else:
        genai.configure(api_key=SITE_CONFIG["GEMINI_API_KEY"])
        GEMINI_API_CONFIGURED = True
except Exception as e:
    logging.critical(f"Could not configure Gemini API. Details: {e}")

stripe.api_key = SITE_CONFIG["STRIPE_SECRET_KEY"]
if not stripe.api_key:
    logging.warning("Stripe Secret Key is not set. Payment flows will fail.")


# --- 2. Database Management ---
# In a real-world application, this should be a proper database (e.g., PostgreSQL, MongoDB).
# This in-memory solution is not thread-safe and is only suitable for development.
DB = { "users": {}, "chats": {}, "site_settings": {"announcement": "Welcome to the secure MythAI!"}, "ads": {} }

def save_database():
    """Saves the in-memory DB to a JSON file atomically."""
    temp_file = f"{DATABASE_FILE}.tmp"
    try:
        with open(temp_file, 'w') as f:
            serializable_db = {
                "users": {uid: user_to_dict(u) for uid, u in DB['users'].items()},
                "chats": DB['chats'],
                "site_settings": DB['site_settings'],
                "ads": DB['ads']
            }
            json.dump(serializable_db, f, indent=4)
        os.replace(temp_file, DATABASE_FILE)
    except Exception as e:
        logging.error(f"Failed to save database: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def load_database():
    """Loads the database from a JSON file if it exists."""
    global DB
    if not os.path.exists(DATABASE_FILE):
        return
    try:
        with open(DATABASE_FILE, 'r') as f:
            data = json.load(f)
            DB['chats'] = data.get('chats', {})
            DB['site_settings'] = data.get('site_settings', {"announcement": ""})
            DB['ads'] = data.get('ads', {})
            DB['users'] = {uid: User.from_dict(u_data) for uid, u_data in data.get('users', {}).items()}
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Could not load database file. Starting fresh. Error: {e}")


# --- 3. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Login required.", "logged_in": False}), 401

class User(UserMixin):
    def __init__(self, id, username, password_hash, role='user', plan='free', account_type='general', daily_messages=0, last_message_date=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.plan = plan
        self.account_type = account_type
        self.daily_messages = daily_messages
        self.last_message_date = last_message_date or datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def get(user_id):
        return DB['users'].get(user_id)

    @staticmethod
    def get_by_username(username):
        for user in DB['users'].values():
            if user.username.lower() == username.lower():
                return user
        return None

    @staticmethod
    def from_dict(data):
        return User(**data)

def user_to_dict(user):
    return {
        'id': user.id, 'username': user.username, 'password_hash': user.password_hash,
        'role': user.role, 'plan': user.plan, 'account_type': user.account_type,
        'daily_messages': user.daily_messages, 'last_message_date': user.last_message_date
    }

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def initialize_database_defaults():
    made_changes = False
    if not User.get_by_username('nameadmin'):
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'adminadminnoob')
        admin = User(id='nameadmin', username='nameadmin', password_hash=generate_password_hash(admin_pass), role='admin', plan='ultra', account_type='general')
        DB['users']['nameadmin'] = admin
        made_changes = True
        logging.info("Created default admin user.")

    if not User.get_by_username('adminexample'):
        ad_pass = 'adpass'
        advertiser = User(id='adminexample', username='adminexample', password_hash=generate_password_hash(ad_pass), role='advertiser', plan='pro', account_type='general')
        DB['users']['adminexample'] = advertiser
        made_changes = True
        logging.info("Created default advertiser user.")

    if made_changes:
        save_database()

load_database()
with app.app_context():
    initialize_database_defaults()


# --- 4. Plan & Rate Limiting Configuration ---
PLAN_CONFIG = {
    "free": {"message_limit": 15, "can_upload": False},
    "pro": {"message_limit": 50, "can_upload": True},
    "ultra": {"message_limit": 10000, "can_upload": True},
    "plus": {"message_limit": 100, "can_upload": True}
}

# Simple in-memory rate limiting
rate_limit_store = {}
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 60  # seconds

# --- 5. Decorators ---
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

def rate_limited(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ip = request.remote_addr
        now = time.time()
        
        rate_limit_store.setdefault(ip, []).append(now)
        # Clean up old entries
        rate_limit_store[ip] = [t for t in rate_limit_store.get(ip, []) if now - t < RATE_LIMIT_WINDOW]

        if len(rate_limit_store.get(ip, [])) >= RATE_LIMIT_MAX_ATTEMPTS:
            return jsonify({"error": "Too many requests. Please try again later."}), 429
            
        return f(*args, **kwargs)
    return decorated_function

# --- 6. Backend Helper Functions ---
def check_and_reset_daily_limit(user):
    """Resets a user's daily message count if the day has changed."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0

def get_user_data_for_frontend(user):
    """Prepares user data for sending to the frontend."""
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_details = PLAN_CONFIG.get(user.plan, PLAN_CONFIG['free'])
    return {
        "id": user.id, "username": user.username, "role": user.role, "plan": user.plan,
        "account_type": user.account_type, "daily_messages": user.daily_messages,
        "message_limit": plan_details["message_limit"], "can_upload": plan_details["can_upload"],
    }

def get_all_user_chats(user_id):
    """Retrieves all chats belonging to a specific user."""
    return {chat_id: chat_data for chat_id, chat_data in DB['chats'].items() if chat_data.get('user_id') == user_id}


# --- 7. Core API Routes (Auth, Status) ---
@app.route('/')
def index():
    # Corrected impersonation logic. This check happens once on the initial load.
    if 'impersonator_id' in session and current_user.is_authenticated and current_user.role != 'admin':
        impersonator = User.get(session['impersonator_id'])
        if impersonator and impersonator.role == 'admin':
            logging.info(f"Admin {impersonator.id} impersonating as {current_user.id}")
            logout_user() # Log out the impersonated user
            login_user(impersonator) # Log the admin back in
        session.pop('impersonator_id', None)
        return redirect(url_for('index'))
    return send_file(HTML_FILE_PATH, mimetype='text/html')

@app.route('/api/config')
def get_config():
    return jsonify({"stripe_public_key": SITE_CONFIG["STRIPE_PUBLIC_KEY"]})

@app.route('/api/signup', methods=['POST'])
@rate_limited
def signup():
    data = request.get_json()
    if not data: return jsonify({"error": "Invalid request format."}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')
    account_type = data.get('account_type', 'general')

    if not all([username, password, account_type]) or len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username (min 3 chars) and password (min 6 chars) are required."}), 400
    if account_type not in ['general', 'plus_user']:
        return jsonify({"error": "Invalid account type."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409

    try:
        new_user = User(id=str(uuid.uuid4()), username=username, password_hash=generate_password_hash(password), account_type=account_type)
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(new_user),
            "chats": {}, "settings": DB['site_settings']
        })
    except Exception as e:
        logging.error(f"Error during signup for {username}: {e}")
        return jsonify({"error": "An internal server error occurred during signup."}), 500

@app.route('/api/login', methods=['POST'])
@rate_limited
def login():
    data = request.get_json()
    if not data: return jsonify({"error": "Invalid request format."}), 400

    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    if user and check_password_hash(user.password_hash, password):
        login_user(user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id) if user.role not in ['admin', 'advertiser'] else {},
            "settings": DB['site_settings']
        })
    return jsonify({"error": "Invalid username or password."}), 401

@app.route('/api/logout')
def logout():
    logout_user()
    return jsonify({"success": True})

@app.route('/api/status')
def status():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True, "user": get_user_data_for_frontend(current_user),
            "chats": get_all_user_chats(current_user.id) if current_user.role not in ['admin', 'advertiser'] else {},
            "settings": DB['site_settings']
        })
    return jsonify({"logged_in": False})

@app.route('/api/special_signup', methods=['POST'])
@rate_limited
def special_signup():
    data = request.get_json()
    if not data: return jsonify({"error": "Invalid request format."}), 400

    username, password, secret_key, role = data.get('username'), data.get('password'), data.get('secret_key'), data.get('role')

    if secret_key != SITE_CONFIG["SECRET_REGISTRATION_KEY"]:
        return jsonify({"error": "Invalid secret key."}), 403
    if role not in ['admin', 'advertiser']:
        return jsonify({"error": "Invalid role specified."}), 400
    if not all([username, password]):
        return jsonify({"error": "Username and password are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409

    new_user = User(id=str(uuid.uuid4()), username=username, password_hash=generate_password_hash(password), role=role, plan='pro')
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({"success": True, "user": get_user_data_for_frontend(new_user)})


# --- 8. Chat API Routes ---
@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api():
    if not GEMINI_API_CONFIGURED:
        return jsonify({"error": "AI services are currently unavailable."}), 503

    try:
        data = request.form
        chat_id = data.get('chat_id')
        prompt = data.get('prompt', '').strip()
        is_plus_mode = data.get('is_plus_mode') == 'true'

        if not chat_id:
            return jsonify({"error": "Missing chat identifier."}), 400

        chat = DB['chats'].get(chat_id)
        if not chat or chat.get('user_id') != current_user.id:
            return jsonify({"error": "Chat not found or access denied."}), 404

        check_and_reset_daily_limit(current_user)
        plan_details = PLAN_CONFIG.get(current_user.plan, PLAN_CONFIG['free'])
        if current_user.daily_messages >= plan_details["message_limit"]:
            return jsonify({"error": f"Daily message limit of {plan_details['message_limit']} reached."}), 429

        current_time = datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')
        base_system_instruction = f"The current date and time is {current_time}. The user is located in Yorkton, Saskatchewan, Canada."
        
        if is_plus_mode and current_user.account_type == 'plus_user':
            persona_instruction = "You are MythAI Plus, a premium, enhanced AI assistant. Your goal is to provide faster, more detailed, and more insightful answers. You have access to advanced tools and knowledge. Be proactive and thorough."
        else:
            persona_instruction = "You are Myth AI, a powerful, general-purpose assistant for creative tasks, coding, and complex questions."
            
        final_system_instruction = f"{base_system_instruction}\n\n{persona_instruction}"

        history = [{"role": "user" if msg['sender'] == 'user' else 'model', "parts": [{"text": msg['content']}]} for msg in chat['messages']]
        
        model_input_parts = []
        if prompt:
            model_input_parts.append({"text": prompt})

        uploaded_file = request.files.get('file')
        if uploaded_file:
            if not plan_details['can_upload']:
                return jsonify({"error": "Your plan does not support file uploads."}), 403
            try:
                img = Image.open(uploaded_file.stream)
                img.thumbnail((512, 512))
                buffered = BytesIO()
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                img.save(buffered, format="JPEG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                model_input_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})
            except Exception as e:
                logging.error(f"Error processing image for chat {chat_id}: {e}")
                return jsonify({"error": "Invalid or unsupported image file."}), 400

        if not model_input_parts:
            return jsonify({"error": "A prompt or file is required."}), 400

        model = genai.GenerativeModel('gemini-1.5-flash-latest', system_instruction=final_system_instruction)
        chat_session = model.start_chat(history=history)

        def generate_chunks():
            full_response_text = ""
            try:
                response_stream = chat_session.send_message(model_input_parts, stream=True)
                for chunk in response_stream:
                    if chunk.text:
                        full_response_text += chunk.text
                        yield chunk.text
            except Exception as e:
                logging.error(f"Error during Gemini stream for chat {chat_id}: {e}")
                yield json.dumps({"error": f"An error occurred with the AI model: {str(e)}"})
                return

            if not full_response_text.strip():
                logging.info(f"Received an empty response for chat {chat_id}.")
                return

            chat['messages'].append({'sender': 'user', 'content': prompt})
            chat['messages'].append({'sender': 'model', 'content': full_response_text})
            current_user.daily_messages += 1

            if len(chat['messages']) <= 2:
                try:
                    title_prompt = f"Summarize the following conversation with a short, descriptive title (4 words max, be concise).\n\nUser: \"{prompt}\"\nAssistant: \"{full_response_text[:200]}\""
                    title_response = genai.GenerativeModel('gemini-1.5-flash-latest').generate_content(title_prompt)
                    title_text = title_response.text.strip().replace('"', '')
                    chat['title'] = title_text if title_text else (prompt[:40] + '...')
                except Exception as title_e:
                    logging.error(f"Could not generate title for chat {chat_id}: {title_e}")
                    chat['title'] = prompt[:40] + '...' if len(prompt) > 40 else "Chat"
            
            save_database()

        return Response(stream_with_context(generate_chunks()), mimetype='text/plain')

    except Exception as e:
        logging.error(f"Fatal error in /api/chat setup for chat {chat_id}: {str(e)}")
        return jsonify({"error": f"An internal server error occurred."}), 500

@app.route('/api/chat/new', methods=['POST'])
@login_required
def new_chat():
    try:
        chat_id = f"chat_{current_user.id}_{datetime.now().timestamp()}"
        new_chat_data = {
            "id": chat_id, "user_id": current_user.id, "title": "New Chat",
            "messages": [], "created_at": datetime.now().isoformat(), "is_public": False
        }
        DB['chats'][chat_id] = new_chat_data
        save_database()
        return jsonify({"success": True, "chat": new_chat_data})
    except Exception as e:
        logging.error(f"Error creating new chat for user {current_user.id}: {e}")
        return jsonify({"error": "Could not create a new chat."}), 500

@app.route('/api/chat/rename', methods=['POST'])
@login_required
def rename_chat():
    data = request.get_json()
    chat_id = data.get('chat_id')
    new_title = data.get('title', '').strip()
    if not all([chat_id, new_title]):
        return jsonify({"error": "Chat ID and new title are required."}), 400

    chat = DB['chats'].get(chat_id)
    if chat and chat.get('user_id') == current_user.id:
        chat['title'] = new_title
        save_database()
        return jsonify({"success": True, "message": "Chat renamed."})
    return jsonify({"error": "Chat not found or access denied."}), 404

@app.route('/api/chat/delete', methods=['POST'])
@login_required
def delete_chat():
    chat_id = request.json.get('chat_id')
    chat = DB['chats'].get(chat_id)
    if chat and chat.get('user_id') == current_user.id:
        del DB['chats'][chat_id]
        save_database()
        return jsonify({"success": True, "message": "Chat deleted."})
    return jsonify({"error": "Chat not found or access denied."}), 404

@app.route('/api/chat/share', methods=['POST'])
@login_required
def share_chat():
    chat_id = request.json.get('chat_id')
    chat = DB['chats'].get(chat_id)
    if chat and chat.get('user_id') == current_user.id:
        chat['is_public'] = True
        save_database()
        return jsonify({"success": True, "share_id": chat_id})
    return jsonify({"error": "Chat not found or access denied."}), 404

# --- 9. Public Share and Payment Routes ---
@app.route('/share/<chat_id>')
def view_shared_chat(chat_id):
    chat = DB['chats'].get(chat_id)
    if not chat or not chat.get('is_public'):
        return "Chat not found or is not public.", 404

    # Use escape() to prevent XSS vulnerabilities when rendering HTML.
    chat_html = f"<html><head><title>{escape(chat['title'])}</title></head><body><h1>{escape(chat['title'])}</h1>"
    for msg in chat['messages']:
        sender = "<b>You:</b>" if msg['sender'] == 'user' else "<b>Myth AI:</b>"
        # Escape the content before rendering.
        content = escape(msg['content']).replace('\n', '<br>')
        chat_html += f"<p>{sender} {content}</p><hr>"
    chat_html += "</body></html>"
    return chat_html

@app.route('/api/plans')
@login_required
def get_plans():
    plans = {
        "free": {"name": "Free", "price_string": "Free", "features": ["15 Daily Messages", "Standard Model Access"], "color": "text-gray-300"},
        "plus": {"name": "MythAI Plus", "price_string": "$4.99 / month", "features": ["100 Daily Messages", "Image Uploads", "Enhanced AI Persona"], "color": "text-cyan-400"},
        "pro": {"name": "Pro", "price_string": "$9.99 / month", "features": ["50 Daily Messages", "Image Uploads", "Priority Support"], "color": "text-indigo-400"},
        "ultra": {"name": "Ultra", "price_string": "$100 one-time", "features": ["Unlimited Messages", "Image Uploads", "Access to All Models"], "color": "text-purple-400"},
    }
    return jsonify({
        "success": True, "plans": plans, "user_plan": current_user.plan,
        "user_account_type": current_user.account_type
    })

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if not stripe.api_key:
        return jsonify(error={'message': 'Payment services are currently unavailable.'}), 500

    plan_id = request.json.get('plan_id')
    price_map = {
        "pro": {"id": SITE_CONFIG["STRIPE_PRO_PRICE_ID"], "mode": "subscription"},
        "ultra": {"id": SITE_CONFIG["STRIPE_ULTRA_PRICE_ID"], "mode": "payment"},
        "plus": {"id": SITE_CONFIG["STRIPE_PLUS_PRICE_ID"], "mode": "subscription"}
    }
    if plan_id not in price_map:
        return jsonify(error={'message': 'Invalid plan selected.'}), 400

    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price': price_map[plan_id]['id'], 'quantity': 1}],
            mode=price_map[plan_id]['mode'],
            success_url=SITE_CONFIG["YOUR_DOMAIN"] + '/?payment=success',
            cancel_url=SITE_CONFIG["YOUR_DOMAIN"] + '/?payment=cancel',
            client_reference_id=current_user.id,
            metadata={'plan_id': plan_id} # Add metadata to the session for the webhook
        )
        return jsonify({'id': checkout_session.id})
    except Exception as e:
        logging.error(f"Stripe session creation failed for user {current_user.id}: {e}")
        return jsonify(error={'message': "Could not create payment session."}), 500


@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Stripe webhook endpoint to handle post-payment events."""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, SITE_CONFIG['STRIPE_WEBHOOK_SECRET']
        )
    except ValueError as e:
        # Invalid payload
        logging.error(f"Invalid Stripe payload: {e}")
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        logging.error(f"Invalid Stripe signature: {e}")
        return 'Invalid signature', 400

    # Handle the checkout.session.completed event
    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        client_reference_id = session_obj.get('client_reference_id')
        plan_id = session_obj.get('metadata', {}).get('plan_id')

        if client_reference_id and plan_id:
            user = User.get(client_reference_id)
            if user:
                user.plan = plan_id
                save_database()
                logging.info(f"User {user.id} plan updated to {plan_id} via webhook.")
            else:
                logging.warning(f"Webhook received for unknown user ID: {client_reference_id}")
    
    return jsonify(success=True)


# --- 10. Admin Routes ---
@app.route('/api/admin_data')
@admin_required
def admin_data():
    all_users_data = []
    stats = {"total_users": 0, "pro_users": 0, "ultra_users": 0, "plus_users": 0}
    for user in DB["users"].values():
        if user.role != 'admin':
            stats['total_users'] += 1
            if user.plan == 'pro': stats['pro_users'] += 1
            elif user.plan == 'ultra': stats['ultra_users'] += 1
            elif user.plan == 'plus': stats['plus_users'] += 1
            
            all_users_data.append({
                "id": user.id, "username": user.username, "plan": user.plan,
                "role": user.role, "account_type": user.account_type
            })
    return jsonify({
        "success": True, "stats": stats,
        "users": sorted(all_users_data, key=lambda x: x['username']),
        "announcement": DB['site_settings']['announcement']
    })

@app.route('/api/admin/delete_user', methods=['POST'])
@admin_required
def admin_delete_user():
    user_id = request.json.get('user_id')
    if user_id == current_user.id:
        return jsonify({"error": "Cannot delete your own account."}), 400
    if user_id in DB['users']:
        del DB['users'][user_id]
        chats_to_delete = [cid for cid, c in DB['chats'].items() if c.get('user_id') == user_id]
        for cid in chats_to_delete: del DB['chats'][cid]
        save_database()
        return jsonify({"success": True, "message": f"User {user_id} and their chats deleted."})
    return jsonify({"error": "User not found."}), 404

@app.route('/api/admin/announcement', methods=['POST'])
@admin_required
def set_announcement():
    text = request.json.get('text', '').strip()
    DB['site_settings']['announcement'] = text
    save_database()
    return jsonify({"success": True, "message": "Announcement updated."})

@app.route('/api/admin/impersonate', methods=['POST'])
@admin_required
def impersonate_user():
    username = request.json.get('username')
    user_to_impersonate = User.get_by_username(username)
    if not user_to_impersonate:
        return jsonify({"error": "User not found."}), 404
    if user_to_impersonate.role == 'admin':
        return jsonify({"error": "Cannot impersonate another admin."}), 403
    
    session['impersonator_id'] = current_user.id
    logout_user()
    login_user(user_to_impersonate, remember=True)
    return jsonify({"success": True, "message": f"Now impersonating {username}"})


# --- Main Execution ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
