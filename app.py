import os
import json
import logging
import base64
import time
from io import BytesIO
from flask import Flask, Response, request, jsonify, session, redirect, url_for, render_template, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
import stripe
from PIL import Image
import fcntl
import platform

# --- 1. Initial Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Security Check for Essential Keys ---
REQUIRED_KEYS = ['SECRET_KEY', 'GEMINI_API_KEY', 'SECRET_REGISTRATION_KEY', 'SECRET_STUDENT_KEY', 'SECRET_TEACHER_KEY']
for key in REQUIRED_KEYS:
    if not os.environ.get(key):
        logging.critical(f"CRITICAL ERROR: Environment variable '{key}' is not set. Application cannot start securely.")
        exit(f"Error: Missing required environment variable '{key}'. Please set it in your .env file.")

# --- Application Setup ---
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
DATABASE_FILE = 'database.json'

# --- Site & API Configuration ---
SITE_CONFIG = {
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    "STRIPE_SECRET_KEY": os.environ.get('STRIPE_SECRET_KEY'),
    "STRIPE_PUBLIC_KEY": os.environ.get('STRIPE_PUBLIC_KEY'),
    "STRIPE_PRO_PRICE_ID": os.environ.get('STRIPE_PRO_PRICE_ID'),
    "STRIPE_ULTRA_PRICE_ID": os.environ.get('STRIPE_ULTRA_PRICE_ID'),
    "STRIPE_STUDENT_PRICE_ID": os.environ.get('STRIPE_STUDENT_PRICE_ID'),
    "YOUR_DOMAIN": os.environ.get('YOUR_DOMAIN', 'http://localhost:5000'),
    "SECRET_REGISTRATION_KEY": os.environ.get('SECRET_REGISTRATION_KEY'),
    "SECRET_STUDENT_KEY": os.environ.get('SECRET_STUDENT_KEY'),
    "SECRET_TEACHER_KEY": os.environ.get('SECRET_TEACHER_KEY'),
}

# --- API Initialization ---
GEMINI_API_CONFIGURED = False
try:
    genai.configure(api_key=SITE_CONFIG["GEMINI_API_KEY"])
    GEMINI_API_CONFIGURED = True
except Exception as e:
    logging.critical(f"Could not configure Gemini API. Details: {e}")

stripe.api_key = SITE_CONFIG["STRIPE_SECRET_KEY"]
if not stripe.api_key:
    logging.warning("Stripe Secret Key is not set. Payment flows will fail.")

# --- 2. Database Management ---
DB = { "users": {}, "chats": {}, "site_settings": {"announcement": "Welcome! Student and Teacher signups are now available."} }

def save_database():
    """Saves the entire in-memory DB to a JSON file atomically with file locking."""
    temp_file = f"{DATABASE_FILE}.tmp"
    try:
        serializable_db = {
            "users": {uid: user_to_dict(u) for uid, u in DB['users'].items()},
            "chats": DB['chats'],
            "site_settings": DB['site_settings'],
        }
        with open(temp_file, 'w') as f:
            if platform.system() != 'Windows':
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(serializable_db, f, indent=4)
        os.replace(temp_file, DATABASE_FILE)
    except Exception as e:
        logging.error(f"Failed to save database: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)
    finally:
        if platform.system() != 'Windows' and 'f' in locals():
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def load_database():
    """Loads the database from a JSON file if it exists."""
    global DB
    if not os.path.exists(DATABASE_FILE):
        return
    try:
        with open(DATABASE_FILE, 'r') as f:
            if platform.system() != 'Windows':
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            data = json.load(f)
            DB['chats'] = data.get('chats', {})
            DB['site_settings'] = data.get('site_settings', {"announcement": ""})
            DB['users'] = {uid: User.from_dict(u_data) for uid, u_data in data.get('users', {}).items()}
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Could not load database file. Starting fresh. Error: {e}")
    finally:
        if platform.system() != 'Windows' and 'f' in locals():
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

# --- 3. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Login required.", "logged_in": False}), 401

class User(UserMixin):
    def __init__(self, id, username, password_hash, role='user', plan='free', account_type='general', daily_messages=0, last_message_date=None, teacher_code=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.plan = plan
        self.account_type = account_type
        self.daily_messages = daily_messages
        self.last_message_date = last_message_date or datetime.now().strftime("%Y-%m-%d")
        self.teacher_code = teacher_code

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
        'daily_messages': user.daily_messages, 'last_message_date': user.last_message_date,
        'teacher_code': user.teacher_code
    }

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def initialize_database_defaults():
    made_changes = False
    if not User.get_by_username('admin'):
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'supersecretadminpassword123')
        admin = User(id='admin', username='admin', password_hash=generate_password_hash(admin_pass), role='admin', plan='ultra', account_type='general')
        DB['users']['admin'] = admin
        made_changes = True
        logging.info("Created default admin user.")
    if made_changes:
        save_database()

load_database()
with app.app_context():
    initialize_database_defaults()

# --- 4. Plan & Rate Limiting Configuration ---
PLAN_CONFIG = {
    "free": {"name": "Free", "price_string": "Free", "features": ["15 Daily Messages", "Standard Model Access"], "color": "text-gray-300", "message_limit": 15, "can_upload": False, "model": "gemini-1.5-flash-latest"},
    "pro": {"name": "Pro", "price_string": "$9.99 / month", "features": ["50 Daily Messages", "Image Uploads", "Priority Support"], "color": "text-indigo-400", "message_limit": 50, "can_upload": True, "model": "gemini-1.5-pro-latest"},
    "ultra": {"name": "Ultra", "price_string": "$100 one-time", "features": ["Unlimited Messages", "Image Uploads", "Access to All Models"], "color": "text-purple-400", "message_limit": 10000, "can_upload": True, "model": "gemini-1.5-pro-latest"},
    "student": {"name": "Student", "price_string": "$4.99 / month", "features": ["100 Daily Messages", "Image Uploads", "Study Buddy Persona"], "color": "text-green-400", "message_limit": 100, "can_upload": True, "model": "gemini-1.5-flash-latest"}
}

rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
HISTORY_LIMIT = 20

def cleanup_rate_limit_store():
    """Cleans up expired rate limit entries."""
    now = time.time()
    for ip in list(rate_limit_store.keys()):
        rate_limit_store[ip] = [t for t in rate_limit_store[ip] if now - t < RATE_LIMIT_WINDOW]
        if not rate_limit_store[ip]:
            del rate_limit_store[ip]

# --- 5. Decorators ---
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

def teacher_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.account_type != 'teacher':
            return jsonify({"error": "Teacher access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

def rate_limited(max_attempts=5):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            cleanup_rate_limit_store()
            ip = request.remote_addr
            now = time.time()
            rate_limit_store[ip] = rate_limit_store.get(ip, [])
            if len(rate_limit_store[ip]) >= max_attempts:
                return jsonify({"error": "Too many requests. Please try again later."}), 429
            rate_limit_store[ip].append(now)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- 6. Backend Helper Functions ---
def check_and_reset_daily_limit(user):
    """Resets a user's daily message count if the day has changed."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0
        save_database()

def get_user_data_for_frontend(user):
    """Prepares user data for sending to the frontend."""
    if not user:
        return {}
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

# --- 7. Core API Routes ---
@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        logging.error(f"Error serving index: {e}")
        return jsonify({"error": "Failed to load the page."}), 500

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/config')
def get_config():
    return jsonify({"stripe_public_key": SITE_CONFIG["STRIPE_PUBLIC_KEY"]})

@app.route('/api/signup', methods=['POST'])
@rate_limited()
def signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request format."}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not all([username, password]) or len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username (min 3 chars) and password (min 6 chars) are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    try:
        new_user = User(id=username, username=username, password_hash=generate_password_hash(password), account_type='general')
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

@app.route('/api/student_signup', methods=['POST'])
@rate_limited()
def student_signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request format."}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '')
    secret_key = data.get('secret_key')
    if secret_key != SITE_CONFIG["SECRET_STUDENT_KEY"]:
        return jsonify({"error": "Invalid student access key."}), 403
    if not all([username, password]) or len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username (min 3 chars) and password (min 6 chars) are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    try:
        new_user = User(id=username, username=username, password_hash=generate_password_hash(password), account_type='student', plan='student')
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(new_user),
            "chats": {}, "settings": DB['site_settings']
        })
    except Exception as e:
        logging.error(f"Error during student signup for {username}: {e}")
        return jsonify({"error": "An internal server error occurred during signup."}), 500

@app.route('/api/teacher_signup', methods=['POST'])
@rate_limited()
def teacher_signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request format."}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '')
    secret_key = data.get('secret_key')
    if secret_key != SITE_CONFIG["SECRET_TEACHER_KEY"]:
        return jsonify({"error": "Invalid teacher access key."}), 403
    if not all([username, password]) or len(username) < 3 or len(password) < 6:
        return jsonify({"error": "Username (min 3 chars) and password (min 6 chars) are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    try:
        new_user = User(id=username, username=username, password_hash=generate_password_hash(password), account_type='teacher', plan='pro')
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(new_user),
            "chats": {}, "settings": DB['site_settings']
        })
    except Exception as e:
        logging.error(f"Error during teacher signup for {username}: {e}")
        return jsonify({"error": "An internal server error occurred during signup."}), 500

@app.route('/api/login', methods=['POST'])
@rate_limited()
def login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request format."}), 400
    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    if user and check_password_hash(user.password_hash, password):
        login_user(user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id) if user.role != 'admin' else {},
            "settings": DB['site_settings']
        })
    return jsonify({"error": "Invalid username or password."}), 401

@app.route('/api/logout')
def logout():
    if 'impersonator_id' in session:
        impersonator = User.get(session.pop('impersonator_id', None))
        if impersonator:
            logout_user()
            login_user(impersonator)
            return redirect(url_for('index'))
    logout_user()
    return jsonify({"success": True})

@app.route('/api/status')
def status():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True, "user": get_user_data_for_frontend(current_user),
            "chats": get_all_user_chats(current_user.id) if current_user.role != 'admin' else {},
            "settings": DB['site_settings']
        })
    return jsonify({"logged_in": False})

@app.route('/api/special_signup', methods=['POST'])
@rate_limited()
def special_signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request format."}), 400
    username, password, secret_key = data.get('username'), data.get('password'), data.get('secret_key')
    if secret_key != SITE_CONFIG["SECRET_REGISTRATION_KEY"]:
        return jsonify({"error": "Invalid secret key."}), 403
    if not all([username, password]):
        return jsonify({"error": "Username and password are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    try:
        new_user = User(id=username, username=username, password_hash=generate_password_hash(password), role='admin', plan='ultra')
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)
        return jsonify({"success": True, "user": get_user_data_for_frontend(new_user)})
    except Exception as e:
        logging.error(f"Error during special signup for {username}: {e}")
        return jsonify({"error": "An internal server error occurred during signup."}), 500

# --- 8. Chat API Routes ---
@app.route('/api/chat', methods=['POST'])
@login_required
@rate_limited(max_attempts=20)
def chat_api():
    if not GEMINI_API_CONFIGURED:
        return jsonify({"error": "AI services are currently unavailable."}), 503
    try:
        data = request.form
        chat_id = data.get('chat_id')
        prompt = data.get('prompt', '').strip()
        is_study_mode = data.get('is_study_mode') == 'true'
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
        base_system_instruction = f"The current date and time is {current_time}. The user is located in Yorkton, Saskatchewan, Canada. Your developer is devector."
        persona_instruction = (
            "You are Study Buddy, a friendly and encouraging tutor. Your goal is to guide the user to the answer without giving it away directly. Ask leading questions, explain concepts simply, and help them break down the problem. Never just provide the final answer."
            if is_study_mode and current_user.account_type == 'student'
            else "You are Myth AI, a powerful, general-purpose assistant for creative tasks, coding, and complex questions."
        )
        final_system_instruction = f"{base_system_instruction}\n\n{persona_instruction}"
        history = [{"role": "user" if msg['sender'] == 'user' else 'model', "parts": [{"text": msg['content']}]} for msg in chat['messages'][-HISTORY_LIMIT:] if msg.get('content')]
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
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(buffered, format="JPEG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                model_input_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})
            except Exception as e:
                logging.error(f"Error processing image for chat {chat_id}: {e}")
                return jsonify({"error": "Invalid or unsupported image file."}), 400
        if not model_input_parts:
            return jsonify({"error": "A prompt or file is required."}), 400
        chat['messages'].append({'sender': 'user', 'content': prompt})
        current_user.daily_messages += 1
        save_database()
        model = genai.GenerativeModel(plan_details['model'], system_instruction=final_system_instruction)
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
                yield json.dumps({"error": f"AI model error: {str(e)}"})
                return
            if not full_response_text.strip():
                full_response_text = "I'm sorry, I couldn't generate a response. Please try again."
                yield full_response_text
            chat['messages'].append({'sender': 'model', 'content': full_response_text})
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
        logging.error(f"Fatal error in /api/chat for chat {chat_id}: {str(e)}")
        return jsonify({"error": "An internal server error occurred."}), 500

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
    chat_html = f"<html><head><title>{chat['title']}</title></head><body><h1>{chat['title']}</h1>"
    for msg in chat['messages']:
        sender = "<b>You:</b>" if msg['sender'] == 'user' else "<b>Myth AI:</b>"
        content = msg['content'].replace('<', '&lt;').replace('>', '&gt;')
        chat_html += f"<p>{sender} {content.replace('/n', '<br>')}</p><hr>"
    chat_html += "</body></html>"
    return chat_html

@app.route('/api/plans')
@login_required
def get_plans():
    plans_for_frontend = {
        plan_id: {
            "name": details["name"], "price_string": details["price_string"],
            "features": details["features"], "color": details["color"]
        } for plan_id, details in PLAN_CONFIG.items()
    }
    return jsonify({
        "success": True, "plans": plans_for_frontend, "user_plan": current_user.plan,
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
        "student": {"id": SITE_CONFIG["STRIPE_STUDENT_PRICE_ID"], "mode": "subscription"}
    }
    if plan_id not in price_map:
        return jsonify(error={'message': 'Invalid plan selected.'}), 400
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price': price_map[plan_id]['id'], 'quantity': 1}],
            mode=price_map[plan_id]['mode'],
            success_url=SITE_CONFIG["YOUR_DOMAIN"] + '/?payment=success',
            cancel_url=SITE_CONFIG["YOUR_DOMAIN"] + '/?payment=cancel',
            client_reference_id=current_user.id
        )
        return jsonify({'id': checkout_session.id})
    except Exception as e:
        logging.error(f"Stripe session creation failed for user {current_user.id}: {e}")
        return jsonify(error={'message': "Could not create payment session."}), 500

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    if not all([payload, sig_header, endpoint_secret]):
        return 'Missing data for webhook', 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return 'Invalid webhook payload or signature', 400
    if event['type'] != 'checkout.session.completed':
        return 'Event not handled', 200
    session = event['data']['object']
    client_reference_id = session.get('client_reference_id')
    user = User.get(client_reference_id)
    if not user:
        logging.error(f"No user found for client_reference_id {client_reference_id}")
        return 'User not found', 400
    line_items = stripe.checkout.Session.list_line_items(session.id, limit=1).data
    if not line_items:
        logging.error(f"No line items found for session {session.id}")
        return 'No line items', 400
    price_id = line_items[0].price.id
    new_plan = None
    if price_id == SITE_CONFIG["STRIPE_PRO_PRICE_ID"]:
        new_plan = 'pro'
    elif price_id == SITE_CONFIG["STRIPE_ULTRA_PRICE_ID"]:
        new_plan = 'ultra'
    elif price_id == SITE_CONFIG["STRIPE_STUDENT_PRICE_ID"]:
        new_plan = 'student'
    if new_plan:
        user.plan = new_plan
        save_database()
        logging.info(f"User {user.id} upgraded to {new_plan} plan via webhook.")
    return 'Success', 200

# --- 10. Admin Routes ---
@app.route('/api/admin_data')
@admin_required
def admin_data():
    all_users_data = []
    stats = {"total_users": 0, "pro_users": 0, "ultra_users": 0}
    for user in DB["users"].values():
        if user.role != 'admin':
            stats['total_users'] += 1
            if user.plan == 'pro':
                stats['pro_users'] += 1
            elif user.plan == 'ultra':
                stats['ultra_users'] += 1
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
    user_id_to_delete = request.json.get('user_id')
    if user_id_to_delete == current_user.id:
        return jsonify({"error": "Cannot delete your own account."}), 400
    user_to_delete = User.get(user_id_to_delete)
    if not user_to_delete:
        return jsonify({"error": "User not found."}), 404
    admin_users = [u for u in DB['users'].values() if u.role == 'admin']
    if user_to_delete.role == 'admin' and len(admin_users) <= 1:
        return jsonify({"error": "Cannot delete the last admin account."}), 403
    del DB['users'][user_id_to_delete]
    chats_to_delete = [cid for cid, c in DB['chats'].items() if c.get('user_id') == user_id_to_delete]
    for cid in chats_to_delete:
        del DB['chats'][cid]
    save_database()
    return jsonify({"success": True, "message": f"User {user_id_to_delete} and their chats deleted."})

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
