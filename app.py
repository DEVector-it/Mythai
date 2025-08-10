import os
import json
import logging
import base64
import time
from io import BytesIO
from flask import Flask, Response, request, stream_with_context, session, jsonify, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
import stripe
from PIL import Image

# --- 1. Initial Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Application Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-and-long-random-key-for-myth-ai-v6-safe')
DATABASE_FILE = 'database.json'

# --- Site & API Configuration ---
# IMPORTANT: For the application to function correctly, you must create a .env file
# in the same directory as this script and add your secret keys.
# Example .env file:
# GEMINI_API_KEY="your_gemini_api_key_here"
# STRIPE_SECRET_KEY="sk_test_your_stripe_secret_key_here"

SITE_CONFIG = {
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
    "STRIPE_SECRET_KEY": os.environ.get('STRIPE_SECRET_KEY'),
    "STRIPE_PUBLIC_KEY": os.environ.get('STRIPE_PUBLIC_KEY', 'pk_test_51Ru4xPBSm9qhr9Ev02LOLySoFIztGhmrgUebvTUJtaRO9TFVJE0GwXSlNe3Nd489WpxmrQNIzIoRAxfuhtE0f24o00e6WUfhCb'),
    
    # IMPORTANT: You must create Products in your Stripe Dashboard and replace these placeholder IDs.
    # Go to your Stripe Dashboard -> Products -> Add Product.
    # For Pro and Plus, create a recurring monthly price. For Ultra, create a one-time price.
    # After creating a product and its price, copy the "Price ID" (e.g., price_123...) here.
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
        
        # Clean up old entries
        rate_limit_store[ip] = [t for t in rate_limit_store.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        
        if len(rate_limit_store.get(ip, [])) >= RATE_LIMIT_MAX_ATTEMPTS:
            return jsonify({"error": "Too many requests. Please try again later."}), 429
            
        rate_limit_store.setdefault(ip, []).append(now)
        return f(*args, **kwargs)
    return decorated_function

# --- 6. HTML, CSS, and JavaScript Frontend ---
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Myth AI</title>
    <meta name="description" content="An advanced, feature-rich AI chat application with multiple personas and user roles.">
    <script src="https://js.stripe.com/v3/"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.2.12/marked.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/2.4.1/purify.min.js"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    fontFamily: { sans: ['Inter', 'sans-serif'], mono: ['Fira Code', 'monospace'] },
                    animation: { 'fade-in': 'fadeIn 0.5s ease-out forwards', 'scale-up': 'scaleUp 0.3s ease-out forwards' },
                    keyframes: {
                        fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
                        scaleUp: { '0%': { transform: 'scale(0.95)', opacity: 0 }, '100%': { transform: 'scale(1)', opacity: 1 } },
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #111827; transition: background-color 0.5s ease; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #1f2937; }
        ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 10px; }
        .glassmorphism { background: rgba(31, 41, 55, 0.5); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
        .brand-gradient { background-image: linear-gradient(to right, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .message-wrapper { animation: fadeIn 0.4s ease-out forwards; }
        pre { position: relative; }
        .copy-code-btn { position: absolute; top: 0.5rem; right: 0.5rem; background-color: #374151; color: white; border: none; padding: 0.25rem 0.5rem; border-radius: 0.25rem; cursor: pointer; opacity: 0; transition: opacity 0.2s; font-size: 0.75rem; }
        pre:hover .copy-code-btn { opacity: 1; }
        #sidebar.hidden { transform: translateX(-100%); }
        /* MythAI Plus Theme */
        .plus-mode { background-color: #0d1e3e; color: #c8dcf7; }
        .plus-mode #sidebar { background: rgba(13, 30, 62, 0.7); }
        .plus-mode #chat-window { color: #c8dcf7; }
        .plus-mode .glassmorphism { background: rgba(20, 47, 92, 0.5); border-color: rgba(67, 126, 235, 0.2); }
        .plus-mode .brand-gradient { background-image: linear-gradient(to right, #2dd4bf, #38bdf8); }
        .plus-mode #send-btn { background-image: linear-gradient(to right, #2dd4bf, #38bdf8); }
        .plus-mode #user-input { color: #c8dcf7; }
        .plus-mode #user-input::placeholder { color: #60a5fa; }
        .plus-mode .message-wrapper .font-bold { color: #93c5fd; }
        .plus-mode .ai-avatar { background-image: linear-gradient(to right, #2dd4bf, #38bdf8); }
        .plus-mode ::-webkit-scrollbar-track { background: #0d1e3e; }
        .plus-mode ::-webkit-scrollbar-thumb { background: #1e40af; }
        .plus-mode #sidebar button:hover { background-color: rgba(30, 64, 175, 0.3); }
        .plus-mode #sidebar .bg-blue-600\\/30 { background-color: rgba(59, 130, 246, 0.4); }
    </style>
</head>
<body class="font-sans text-gray-200 antialiased">
    <div id="announcement-banner" class="hidden text-center p-2 bg-indigo-600 text-white text-sm"></div>
    <div id="app-container" class="relative h-screen w-screen"></div>
    <div id="modal-container"></div>
    <div id="toast-container" class="fixed top-6 right-6 z-[100] flex flex-col gap-2"></div>

    <!-- TEMPLATES START HERE -->
    <template id="template-logo">
        <svg width="48" height="48" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
            <defs>
                <linearGradient id="logoGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" style="stop-color:#3b82f6;" />
                    <stop offset="100%" style="stop-color:#8b5cf6;" />
                </linearGradient>
            </defs>
            <path d="M50 10 C 27.9 10 10 27.9 10 50 C 10 72.1 27.9 90 50 90 C 72.1 90 90 72.1 90 50 C 90 27.9 72.1 10 50 10 Z M 50 15 C 69.3 15 85 30.7 85 50 C 85 69.3 69.3 85 50 85 C 30.7 85 15 69.3 15 50 C 15 30.7 30.7 15 50 15 Z" fill="url(#logoGradient)"/>
            <path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
    </template>

    <template id="template-auth-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="auth-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2" id="auth-title">Welcome Back</h2>
                <p class="text-gray-400 text-center mb-8" id="auth-subtitle">Sign in to continue to Myth AI.</p>
                <form id="auth-form">
                    <div class="mb-4">
                        <label for="username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <div class="mb-6">
                        <label for="password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <div id="signup-fields" class="hidden">
                        <div class="mb-4">
                            <label for="account_type" class="block text-sm font-medium text-gray-300 mb-1">Account Type</label>
                             <select id="account_type" name="account_type" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600">
                                <option value="general">General User</option>
                                <option value="plus_user">MythAI Plus User</option>
                            </select>
                        </div>
                    </div>
                    <button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg transition-opacity">Login</button>
                    <p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
                <div class="text-center mt-6">
                    <button id="auth-toggle-btn" class="text-sm text-blue-400 hover:text-blue-300">Don't have an account? Sign Up</button>
                </div>
            </div>
             <div class="text-center mt-4 flex justify-center gap-4">
                <button id="special-auth-link" class="text-xs text-gray-500 hover:text-gray-400">Admin & Ad Portal</button>
            </div>
        </div>
    </template>
    
    <template id="template-app-wrapper">
        <div id="main-app-layout" class="flex h-full w-full transition-colors duration-500">
            <aside id="sidebar" class="bg-gray-900/70 backdrop-blur-lg w-72 flex-shrink-0 flex flex-col p-2 h-full absolute md:relative z-20 transform transition-transform duration-300 ease-in-out -translate-x-full md:translate-x-0">
                <div class="flex-shrink-0 p-2 mb-2 flex items-center gap-3">
                    <div id="app-logo-container"></div>
                    <h1 class="text-2xl font-bold brand-gradient">Myth AI</h1>
                </div>
                <div id="plus-mode-toggle-container" class="hidden flex-shrink-0 p-2 mb-2"></div>
                
                <div class="flex-shrink-0"><button id="new-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/50 transition-colors duration-200"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14" /><path d="M5 12h14" /></svg> New Chat</button></div>
                <div id="chat-history-list" class="flex-grow overflow-y-auto my-4 space-y-1 pr-1"></div>
                
                <!-- AdSense Placeholder Start -->
                <div id="adsense-container" class="p-2 mt-auto">
                    <!-- Your Google AdSense ad unit code goes here -->
                    <!-- Example: -->
                    <!-- 
                    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-your_client_id"
                         crossorigin="anonymous"></script>
                    <ins class="adsbygoogle"
                         style="display:block"
                         data-ad-client="ca-pub-your_client_id"
                         data-ad-slot="your_ad_slot_id"
                         data-ad-format="auto"
                         data-full-width-responsive="true"></ins>
                    <script>
                         (adsbygoogle = window.adsbygoogle || []).push({});
                    </script>
                    -->
                    <div class="w-full h-24 bg-gray-700/50 rounded-lg flex items-center justify-center text-gray-400 text-sm">
                        Ad Placeholder
                    </div>
                </div>
                <!-- AdSense Placeholder End -->

                <div class="flex-shrink-0 border-t border-gray-700 pt-2 space-y-1">
                    <div id="user-info" class="p-3 text-sm"></div>
                    <button id="upgrade-plan-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-indigo-500/20 text-indigo-400 transition-colors duration-200"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6v12m-6-6h12"/></svg> Upgrade Plan</button>
                    <button id="logout-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-red-500/20 text-red-400 transition-colors duration-200"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" x2="9" y1="12" y2="12" /></svg> Logout</button>
                </div>
            </aside>
            <div id="sidebar-backdrop" class="fixed inset-0 bg-black/60 z-10 hidden md:hidden"></div>
            <main class="flex-1 flex flex-col bg-gray-800 h-full">
                <header class="flex-shrink-0 p-4 flex items-center justify-between border-b border-gray-700/50">
                    <div class="flex items-center gap-2">
                        <button id="menu-toggle-btn" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors md:hidden">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
                        </button>
                        <h2 id="chat-title" class="text-xl font-semibold truncate">New Chat</h2>
                    </div>
                    <div class="flex items-center gap-4">
                         <button id="share-chat-btn" title="Share Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button>
                        <button id="rename-chat-btn" title="Rename Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg></button>
                        <button id="delete-chat-btn" title="Delete Chat" class="p-2 rounded-lg hover:bg-red-500/20 text-red-400 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" /></svg></button>
                    </div>
                </header>
                <div id="chat-window" class="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 min-h-0"></div>
                <div class="flex-shrink-0 p-2 md:p-4 md:px-6 border-t border-gray-700/50">
                    <div class="max-w-4xl mx-auto">
                        <div id="stop-generating-container" class="text-center mb-2" style="display: none;">
                            <button id="stop-generating-btn" class="bg-red-600/50 hover:bg-red-600/80 text-white font-semibold py-2 px-4 rounded-lg transition-colors flex items-center gap-2 mx-auto"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><rect width="10" height="10" x="3" y="3" rx="1"/></svg> Stop Generating</button>
                        </div>
                        <div class="relative glassmorphism rounded-2xl shadow-lg">
                            <div id="preview-container" class="hidden p-2 border-b border-gray-600"></div>
                            <textarea id="user-input" placeholder="Message Myth AI..." class="w-full bg-transparent p-4 pl-14 pr-16 resize-none rounded-2xl focus:outline-none" rows="1"></textarea>
                            <div class="absolute left-3 top-1/2 -translate-y-1/2 flex items-center">
                                <button id="upload-btn" title="Upload Image" class="p-2 rounded-full hover:bg-gray-600/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.2 15c.7-1.2 1-2.5.7-3.9-.6-2.4-2.4-4.2-4.8-4.8-1.4-.3-2.7 0-3.9.7L12 8l-1.2-1.1c-1.2-.7-2.5-1-3.9-.7-2.4.6-4.2 2.4-4.8 4.8-.3 1.4 0 2.7.7 3.9L4 16.1M12 13l2 3h-4l2-3z"/><circle cx="12" cy="12" r="10"/></svg></button>
                                <input type="file" id="file-input" class="hidden" accept="image/png, image/jpeg, image/webp">
                            </div>
                            <div class="absolute right-3 top-1/2 -translate-y-1/2 flex items-center">
                                <button id="send-btn" class="p-2 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 transition-opacity disabled:from-gray-500 disabled:to-gray-600 disabled:cursor-not-allowed"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M2 22l20-10L2 2z"/></svg></button>
                            </div>
                        </div>
                         <div class="text-xs text-gray-400 mt-2 text-center" id="message-limit-display"></div>
                    </div>
                </div>
            </main>
        </div>
    </template>

    <template id="template-upgrade-page">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex justify-between items-center mb-8">
                <h1 class="text-3xl font-bold brand-gradient">Choose Your Plan</h1>
                <button id="back-to-chat-btn" class="bg-gray-700 hover:bg-gray-600 text-white font-bold py-2 px-4 rounded-lg transition-colors">Back to Chat</button>
            </header>
            <div id="plans-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8 max-w-6xl mx-auto">
                <!-- Plan cards will be injected here by JS -->
            </div>
        </div>
    </template>
    
    <template id="template-admin-dashboard">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex flex-wrap justify-between items-center gap-4 mb-8">
                <div class="flex items-center gap-4">
                    <div id="admin-logo-container"></div>
                    <h1 class="text-3xl font-bold brand-gradient">Admin Dashboard</h1>
                </div>
                <div>
                    <button id="admin-impersonate-btn" class="bg-yellow-600 hover:bg-yellow-500 text-white font-bold py-2 px-4 rounded-lg transition-colors mr-2">Impersonate User</button>
                    <button id="admin-logout-btn" class="bg-red-600 hover:bg-red-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Logout</button>
                </div>
            </header>

            <div class="mb-8 p-6 glassmorphism rounded-lg">
                <h2 class="text-xl font-semibold mb-4 text-white">Site Announcement</h2>
                <form id="announcement-form" class="flex flex-col sm:flex-row gap-2">
                    <input id="announcement-input" type="text" placeholder="Enter announcement text (leave empty to clear)" class="flex-grow p-2 bg-gray-700/50 rounded-lg border border-gray-600">
                    <button type="submit" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold px-4 py-2 rounded-lg">Set Banner</button>
                </form>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Total Users</h2><p id="admin-total-users" class="text-4xl font-bold text-white">0</p></div>
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Pro Users</h2><p id="admin-pro-users" class="text-4xl font-bold text-white">0</p></div>
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Ultra Users</h2><p id="admin-ultra-users" class="text-4xl font-bold text-white">0</p></div>
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Plus Users</h2><p id="admin-plus-users" class="text-4xl font-bold text-white">0</p></div>
            </div>

            <div class="p-6 glassmorphism rounded-lg">
                <h2 class="text-xl font-semibold mb-4 text-white">User Management</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead class="border-b border-gray-600">
                            <tr>
                                <th class="p-2">Username</th>
                                <th class="p-2">Role</th>
                                <th class="p-2">Plan</th>
                                <th class="p-2">Account Type</th>
                                <th class="p-2">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="admin-user-list"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </template>
    
    <template id="template-modal">
        <div class="modal-backdrop fixed inset-0 bg-black/60 animate-fade-in"></div>
        <div class="modal-content fixed inset-0 flex items-center justify-center p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up relative">
                <button class="close-modal-btn absolute top-4 right-4 text-gray-400 hover:text-white text-3xl leading-none">&times;</button>
                <h3 id="modal-title" class="text-2xl font-bold text-center mb-4">Modal Title</h3>
                <div id="modal-body" class="text-center text-gray-300">Modal content goes here.</div>
            </div>
        </div>
    </template>

    <template id="template-welcome-screen">
        <div class="flex flex-col items-center justify-center h-full text-center p-4 animate-fade-in">
            <div class="w-24 h-24 mb-6" id="welcome-logo-container"></div>
            <h2 id="welcome-title" class="text-3xl md:text-4xl font-bold mb-4">Welcome to Myth AI</h2>
            <p id="welcome-subtitle" class="text-gray-400 max-w-md">Start a new conversation or select one from the sidebar. How can I help you today?</p>
        </div>
    </template>
    
    <template id="template-special-auth-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="special-auth-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2">Special Access Signup</h2>
                <p class="text-gray-400 text-center mb-8">Create an Admin or Advertiser account.</p>
                <form id="special-auth-form">
                    <div class="mb-4">
                        <label for="special-username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="special-username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="special-password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="special-password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="secret-key" class="block text-sm font-medium text-gray-300 mb-1">Secret Key</label>
                        <input type="password" id="secret-key" name="secret_key" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-6">
                        <label for="role-select" class="block text-sm font-medium text-gray-300 mb-1">Account Type</label>
                        <select id="role-select" name="role" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600">
                            <option value="admin">Admin</option>
                            <option value="advertiser">Advertiser</option>
                        </select>
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-purple-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg">Create Account</button>
                    <p id="special-auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
            </div>
            <div class="text-center mt-4">
                <button id="back-to-main-login" class="text-xs text-gray-500 hover:text-gray-400">Back to Main Login</button>
            </div>
        </div>
    </template>
    
    <template id="template-ad-dashboard">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex justify-between items-center mb-8">
                <h1 class="text-3xl font-bold brand-gradient">Advertiser Dashboard</h1>
                <button id="ad-logout-btn" class="bg-red-600 hover:bg-red-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Logout</button>
            </header>
            <div class="max-w-4xl mx-auto glassmorphism rounded-lg p-8">
                <h2 class="text-2xl font-bold text-white mb-4">Welcome, Advertiser!</h2>
                <p class="text-gray-300">This is a placeholder for your ad campaign management tools.</p>
            </div>
        </div>
    </template>
    <!-- TEMPLATES END HERE -->

<script>
/****************************************************************************
 * JAVASCRIPT FRONTEND LOGIC (MYTH AI V6 - SECURE)
 ****************************************************************************/
document.addEventListener('DOMContentLoaded', () => {
    const appState = {
        chats: {}, activeChatId: null, isAITyping: false,
        abortController: null, currentUser: null,
        isPlusMode: false, uploadedFile: null,
    };

    const DOMElements = {
        appContainer: document.getElementById('app-container'),
        modalContainer: document.getElementById('modal-container'),
        toastContainer: document.getElementById('toast-container'),
        announcementBanner: document.getElementById('announcement-banner'),
    };

    // --- UTILITY FUNCTIONS ---
    function showToast(message, type = 'info') {
        const colors = { info: 'bg-blue-600', success: 'bg-green-600', error: 'bg-red-600' };
        const toast = document.createElement('div');
        toast.className = `toast text-white text-sm py-2 px-4 rounded-lg shadow-lg animate-fade-in ${colors[type]}`;
        toast.textContent = message;
        DOMElements.toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    function renderLogo(containerId) {
        const logoTemplate = document.getElementById('template-logo');
        const container = document.getElementById(containerId);
        if (container && logoTemplate) {
            container.innerHTML = '';
            container.appendChild(logoTemplate.content.cloneNode(true));
        }
    }

    async function apiCall(endpoint, options = {}) {
        try {
            const response = await fetch(endpoint, options);
            const data = response.headers.get("Content-Type")?.includes("application/json") ? await response.json() : null;
            
            if (!response.ok) {
                if (response.status === 401) handleLogout(false);
                throw new Error(data?.error || `Server error: ${response.statusText}`);
            }
            return { success: true, ...(data || {}) };
        } catch (error) {
            console.error("API Call Error:", error);
            showToast(error.message, 'error');
            return { success: false, error: error.message };
        }
    }

    function openModal(title, bodyContent, onConfirm, confirmText = 'Confirm') {
        const template = document.getElementById('template-modal');
        const modalWrapper = document.createElement('div');
        modalWrapper.id = 'modal-instance';
        modalWrapper.appendChild(template.content.cloneNode(true));
        DOMElements.modalContainer.appendChild(modalWrapper);
        modalWrapper.querySelector('#modal-title').textContent = title;
        const modalBody = modalWrapper.querySelector('#modal-body');
        if (typeof bodyContent === 'string') {
            modalBody.innerHTML = `<p>${bodyContent}</p>`;
        } else {
            modalBody.innerHTML = '';
            modalBody.appendChild(bodyContent);
        }
        if (onConfirm) {
            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'w-full mt-6 bg-blue-600 hover:bg-blue-500 text-white font-bold py-2 px-4 rounded-lg';
            confirmBtn.textContent = confirmText;
            confirmBtn.onclick = () => { onConfirm(); closeModal(); };
            modalBody.appendChild(confirmBtn);
        }
        const closeModal = () => modalWrapper.remove();
        modalWrapper.querySelector('.close-modal-btn').addEventListener('click', closeModal);
        modalWrapper.querySelector('.modal-backdrop').addEventListener('click', closeModal);
    }

    function closeModal() {
        document.getElementById('modal-instance')?.remove();
    }

    // --- AUTHENTICATION & INITIALIZATION ---
    function renderAuthPage(isLogin = true) {
        const template = document.getElementById('template-auth-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('auth-logo-container');
        
        const signupFields = document.getElementById('signup-fields');
        if (isLogin) {
            document.getElementById('auth-title').textContent = 'Welcome Back';
            document.getElementById('auth-subtitle').textContent = 'Sign in to continue to Myth AI.';
            document.getElementById('auth-submit-btn').textContent = 'Login';
            document.getElementById('auth-toggle-btn').textContent = "Don't have an account? Sign Up";
            signupFields.classList.add('hidden');
        } else {
            document.getElementById('auth-title').textContent = 'Create Account';
            document.getElementById('auth-subtitle').textContent = 'Join Myth AI to get started.';
            document.getElementById('auth-submit-btn').textContent = 'Sign Up';
            document.getElementById('auth-toggle-btn').textContent = 'Already have an account? Login';
            signupFields.classList.remove('hidden');
        }

        document.getElementById('auth-toggle-btn').onclick = () => renderAuthPage(!isLogin);
        document.getElementById('auth-form').onsubmit = async (e) => {
            e.preventDefault();
            const form = e.target;
            const errorEl = document.getElementById('auth-error');
            errorEl.textContent = '';
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            const endpoint = isLogin ? '/api/login' : '/api/signup';
            
            const result = await apiCall(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
            if (result.success) {
                initializeApp(result.user, result.chats, result.settings);
            } else {
                errorEl.textContent = result.error;
            }
        };
        document.getElementById('special-auth-link').onclick = renderSpecialAuthPage;
    }
    
    function renderSpecialAuthPage() {
        const template = document.getElementById('template-special-auth-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('special-auth-logo-container');
        document.getElementById('back-to-main-login').onclick = () => renderAuthPage(true);
        const form = document.getElementById('special-auth-form');
        form.onsubmit = async (e) => {
            e.preventDefault();
            const errorEl = document.getElementById('special-auth-error');
            errorEl.textContent = '';
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            const result = await apiCall('/api/special_signup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });
            if (result.success) {
                initializeApp(result.user, {}, {});
            } else {
                errorEl.textContent = result.error;
            }
        };
    }

    async function checkLoginStatus() {
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('payment') === 'success') {
            showToast('Upgrade successful!', 'success');
            window.history.replaceState({}, document.title, "/");
        } else if (urlParams.get('payment') === 'cancel') {
            showToast('Payment was cancelled.', 'info');
            window.history.replaceState({}, document.title, "/");
        }

        const result = await apiCall('/api/status');
        if (result.success && result.logged_in) {
            initializeApp(result.user, result.chats, result.settings);
        } else {
            renderAuthPage();
        }
    }

    function initializeApp(user, chats, settings) {
        appState.currentUser = user;
        appState.chats = chats;
        if (settings.announcement) {
            DOMElements.announcementBanner.textContent = settings.announcement;
            DOMElements.announcementBanner.classList.remove('hidden');
        } else {
            DOMElements.announcementBanner.classList.add('hidden');
        }
        if (user.role === 'admin') {
            renderAdminDashboard();
        } else if (user.role === 'advertiser') {
            renderAdDashboard();
        } else {
            renderAppUI();
        }
    }

    // --- UI RENDERING ---
    function renderAppUI() {
        const template = document.getElementById('template-app-wrapper');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('app-logo-container');
        
        const sortedChatIds = Object.keys(appState.chats).sort((a, b) =>
            appState.chats[b].created_at.localeCompare(appState.chats[a].created_at)
        );
        appState.activeChatId = sortedChatIds.length > 0 ? sortedChatIds[0] : null;

        renderChatHistoryList();
        renderActiveChat();
        updateUserInfo();
        setupAppEventListeners();
        renderPlusModeToggle();
    }

    function renderActiveChat() {
        const chatWindow = document.getElementById('chat-window');
        const chatTitle = document.getElementById('chat-title');
        if (!chatWindow || !chatTitle) return;

        chatWindow.innerHTML = '';
        appState.uploadedFile = null;
        updatePreviewContainer();

        const chat = appState.chats[appState.activeChatId];
        if (chat && chat.messages.length > 0) {
            chatTitle.textContent = chat.title;
            chat.messages.forEach(msg => addMessageToDOM(msg));
            renderCodeCopyButtons();
        } else {
            chatTitle.textContent = 'New Chat';
            renderWelcomeScreen();
        }
        updateUIState();
    }

    function renderWelcomeScreen() {
        const chatWindow = document.getElementById('chat-window');
        if (!chatWindow) return;
        const template = document.getElementById('template-welcome-screen');
        chatWindow.innerHTML = '';
        chatWindow.appendChild(template.content.cloneNode(true));
        renderLogo('welcome-logo-container');
        
        if (appState.isPlusMode) {
            document.getElementById('welcome-title').textContent = "Welcome to MythAI Plus!";
            document.getElementById('welcome-subtitle').textContent = "You have access to our enhanced AI. How can I assist you?";
        } else {
            document.getElementById('welcome-title').textContent = "Welcome to Myth AI";
            document.getElementById('welcome-subtitle').textContent = "How can I help you today?";
        }
    }

    function renderChatHistoryList() {
        const listEl = document.getElementById('chat-history-list');
        if (!listEl) return;
        listEl.innerHTML = '';
        Object.values(appState.chats)
            .sort((a, b) => b.created_at.localeCompare(a.created_at))
            .forEach(chat => {
                const item = document.createElement('button');
                item.className = `w-full text-left p-3 rounded-lg hover:bg-gray-700/50 transition-colors duration-200 truncate text-sm ${chat.id === appState.activeChatId ? 'bg-blue-600/30 font-semibold' : ''}`;
                item.textContent = chat.title;
                item.onclick = () => {
                    appState.activeChatId = chat.id;
                    renderActiveChat();
                    renderChatHistoryList();
                    const menuToggleBtn = document.getElementById('menu-toggle-btn');
                    if (menuToggleBtn && menuToggleBtn.offsetParent !== null) {
                        document.getElementById('sidebar')?.classList.add('-translate-x-full');
                        document.getElementById('sidebar-backdrop')?.classList.add('hidden');
                    }
                };
                listEl.appendChild(item);
            });
    }

    function updateUserInfo() {
        const userInfoDiv = document.getElementById('user-info');
        if (!userInfoDiv || !appState.currentUser) return;

        const { username, plan, account_type } = appState.currentUser;
        const planName = plan.charAt(0).toUpperCase() + plan.slice(1);
        const planColor = (plan === 'pro' || plan === 'ultra' || plan === 'plus') ? 'text-indigo-400' : 'text-gray-400';
        const avatarColor = `hsl(${username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;
        
        userInfoDiv.innerHTML = `
            <div class="flex items-center gap-3">
                <div class="flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center font-bold text-white" style="background-color: ${avatarColor};">
                    ${username[0].toUpperCase()}
                </div>
                <div>
                    <div class="font-semibold">${username}</div>
                    <div class="text-xs ${planColor}">${planName} Plan (${account_type.replace('_', ' ')})</div>
                </div>
            </div>`;
        
        const limitDisplay = document.getElementById('message-limit-display');
        if(limitDisplay) limitDisplay.textContent = `Daily Messages: ${appState.currentUser.daily_messages} / ${appState.currentUser.message_limit}`;
    }

    function updateUIState() {
        const sendBtn = document.getElementById('send-btn');
        const stopContainer = document.getElementById('stop-generating-container');
        const chatActionButtons = ['share-chat-btn', 'rename-chat-btn', 'delete-chat-btn'];
        const uploadBtn = document.getElementById('upload-btn');

        if (sendBtn) sendBtn.disabled = appState.isAITyping;
        if (stopContainer) stopContainer.style.display = appState.isAITyping ? 'block' : 'none';
        
        const chatExists = !!appState.activeChatId;
        chatActionButtons.forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.style.display = chatExists ? 'flex' : 'none';
        });

        if (uploadBtn) {
            uploadBtn.style.display = appState.currentUser.can_upload ? 'block' : 'none';
        }
    }

    function renderPlusModeToggle() {
        const container = document.getElementById('plus-mode-toggle-container');
        if (!container || appState.currentUser.account_type !== 'plus_user') return;

        container.classList.remove('hidden');
        container.innerHTML = `
            <label for="plus-mode-toggle" class="flex items-center cursor-pointer">
                <div class="relative">
                    <input type="checkbox" id="plus-mode-toggle" class="sr-only">
                    <div class="block bg-gray-600 w-14 h-8 rounded-full"></div>
                    <div class="dot absolute left-1 top-1 bg-white w-6 h-6 rounded-full transition"></div>
                </div>
                <div class="ml-3 font-medium">MythAI Plus Mode</div>
            </label>
        `;
        const toggle = document.getElementById('plus-mode-toggle');
        toggle.addEventListener('change', () => {
            appState.isPlusMode = toggle.checked;
            document.getElementById('main-app-layout').classList.toggle('plus-mode', appState.isPlusMode);
            renderActiveChat();
        });
    }
    
    function updatePreviewContainer() {
        const previewContainer = document.getElementById('preview-container');
        if (!previewContainer) return;

        if (appState.uploadedFile) {
            previewContainer.classList.remove('hidden');
            const objectURL = URL.createObjectURL(appState.uploadedFile);
            previewContainer.innerHTML = `
                <div class="relative inline-block">
                    <img src="${objectURL}" alt="Image preview" class="h-16 w-16 object-cover rounded-md">
                    <button id="remove-preview-btn" class="absolute -top-2 -right-2 bg-red-600 text-white rounded-full w-5 h-5 flex items-center justify-center text-xs">&times;</button>
                </div>
            `;
            document.getElementById('remove-preview-btn').onclick = () => {
                appState.uploadedFile = null;
                document.getElementById('file-input').value = '';
                updatePreviewContainer();
            };
        } else {
            previewContainer.classList.add('hidden');
            previewContainer.innerHTML = '';
        }
    }

    // --- CHAT LOGIC ---
    async function handleSendMessage() {
        const userInput = document.getElementById('user-input');
        if (!userInput) return;
        const prompt = userInput.value.trim();
        if ((!prompt && !appState.uploadedFile) || appState.isAITyping) return;

        if (!appState.activeChatId) {
            const chatCreated = await createNewChat(false);
            if (!chatCreated) {
                showToast("Could not start a new chat session.", "error");
                return;
            }
        }
        
        if (appState.chats[appState.activeChatId]?.messages.length === 0) {
            document.getElementById('chat-window').innerHTML = '';
        }

        addMessageToDOM({ sender: 'user', content: prompt });
        userInput.value = '';
        userInput.style.height = 'auto';
        
        const fileToSend = appState.uploadedFile;
        appState.uploadedFile = null;
        updatePreviewContainer();

        appState.isAITyping = true;
        appState.abortController = new AbortController();
        updateUIState();

        const aiMessage = { sender: 'model', content: '' };
        const aiContentEl = addMessageToDOM(aiMessage, true).querySelector('.message-content');

        try {
            const formData = new FormData();
            formData.append('chat_id', appState.activeChatId);
            formData.append('prompt', prompt);
            formData.append('is_plus_mode', appState.isPlusMode);
            if (fileToSend) {
                formData.append('file', fileToSend);
            }

            const response = await fetch('/api/chat', {
                method: 'POST',
                body: formData,
                signal: appState.abortController.signal,
            });

            if (!response.ok) {
                const errorData = await response.json();
                if (response.status === 401 && !errorData.logged_in) handleLogout(false);
                throw new Error(errorData.error || `Server error: ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let fullResponse = '';
            const chatWindow = document.getElementById('chat-window');

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                const chunk = decoder.decode(value, {stream: true});
                fullResponse += chunk;
                aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse + '<span class="animate-pulse">▍</span>'));
                if(chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
            }
            aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse));
            renderCodeCopyButtons();

            const updatedData = await apiCall('/api/status');
            if (updatedData.success) {
                appState.currentUser = updatedData.user;
                appState.chats = updatedData.chats;
                renderChatHistoryList();
                updateUserInfo();
                document.getElementById('chat-title').textContent = appState.chats[appState.activeChatId].title;
            }
        } catch (err) {
            if (err.name !== 'AbortError') {
                if (aiContentEl) aiContentEl.innerHTML = `<p class="text-red-400 mt-2"><strong>Error:</strong> ${err.message}</p>`;
                showToast(err.message, 'error');
            }
        } finally {
            appState.isAITyping = false;
            appState.abortController = null;
            updateUIState();
        }
    }

    function addMessageToDOM(msg, isStreaming = false) {
        const chatWindow = document.getElementById('chat-window');
        if (!chatWindow || !appState.currentUser) return null;

        const wrapper = document.createElement('div');
        wrapper.className = 'message-wrapper flex items-start gap-4';
        const senderIsAI = msg.sender === 'model';
        const avatarChar = senderIsAI ? 'M' : appState.currentUser.username[0].toUpperCase();
        const userAvatarColor = `background-color: hsl(${appState.currentUser.username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;

        const aiAvatarSVG = `<svg width="20" height="20" viewBox="0 0 100 100"><path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="8" fill="none"/></svg>`;
        const userAvatarHTML = `<div class="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white" style="${userAvatarColor}">${avatarChar}</div>`;
        const aiAvatarHTML = `<div class="ai-avatar flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white bg-gradient-to-br from-blue-500 to-indigo-600">${aiAvatarSVG}</div>`;

        wrapper.innerHTML = `
            ${senderIsAI ? aiAvatarHTML : userAvatarHTML}
            <div class="flex-1 min-w-0">
                <div class="font-bold">${senderIsAI ? (appState.isPlusMode ? 'MythAI Plus' : 'Myth AI') : 'You'}</div>
                <div class="prose prose-invert max-w-none message-content">
                    ${isStreaming ? '<span class="animate-pulse">...</span>' : DOMPurify.sanitize(marked.parse(msg.content))}
                </div>
            </div>`;
        chatWindow.appendChild(wrapper);
        chatWindow.scrollTop = chatWindow.scrollHeight;
        return wrapper;
    }
    
    async function createNewChat(shouldRender = true) {
        const result = await apiCall('/api/chat/new', { method: 'POST' });
        if (result.success) {
            appState.chats[result.chat.id] = result.chat;
            appState.activeChatId = result.chat.id;
            if (shouldRender) {
                renderActiveChat();
                renderChatHistoryList();
            }
            return true;
        }
        return false;
    }

    function renderCodeCopyButtons() {
        document.querySelectorAll('pre').forEach(pre => {
            if (pre.querySelector('.copy-code-btn')) return;
            const button = document.createElement('button');
            button.className = 'copy-code-btn';
            button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
            button.onclick = () => {
                navigator.clipboard.writeText(pre.querySelector('code')?.innerText || '').then(() => {
                    button.innerHTML = 'Copied!';
                    setTimeout(() => button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>', 2000);
                });
            };
            pre.appendChild(button);
        });
    }

    // --- EVENT LISTENERS & HANDLERS ---
    function setupAppEventListeners() {
        const appContainer = document.getElementById('app-container');
        if (!appContainer) return;
        
        appContainer.onclick = (e) => {
            const target = e.target.closest('button');
            if (!target) return;
            switch (target.id) {
                case 'new-chat-btn': createNewChat(true); break;
                case 'logout-btn': handleLogout(); break;
                case 'ad-logout-btn': handleLogout(); break;
                case 'send-btn': handleSendMessage(); break;
                case 'stop-generating-btn': appState.abortController?.abort(); break;
                case 'rename-chat-btn': handleRenameChat(); break;
                case 'delete-chat-btn': handleDeleteChat(); break;
                case 'share-chat-btn': handleShareChat(); break;
                case 'upgrade-plan-btn': renderUpgradePage(); break;
                case 'back-to-chat-btn': renderAppUI(); break;
                case 'upload-btn': document.getElementById('file-input').click(); break;
                case 'menu-toggle-btn': 
                    document.getElementById('sidebar')?.classList.toggle('-translate-x-full');
                    document.getElementById('sidebar-backdrop')?.classList.toggle('hidden');
                    break;
            }
        };

        const userInput = document.getElementById('user-input');
        if (userInput) {
            userInput.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); } };
            userInput.oninput = () => { userInput.style.height = 'auto'; userInput.style.height = `${userInput.scrollHeight}px`; };
        }
        
        const backdrop = document.getElementById('sidebar-backdrop');
        if (backdrop) {
            backdrop.onclick = () => {
                document.getElementById('sidebar')?.classList.add('-translate-x-full');
                backdrop.classList.add('hidden');
            };
        }

        const fileInput = document.getElementById('file-input');
        if (fileInput) {
            fileInput.onchange = (e) => {
                if (e.target.files.length > 0) {
                    appState.uploadedFile = e.target.files[0];
                    updatePreviewContainer();
                }
            };
        }
    }

    async function handleLogout(doApiCall = true) {
        if(doApiCall) await fetch('/api/logout');
        appState.currentUser = null;
        appState.chats = {};
        appState.activeChatId = null;
        DOMElements.announcementBanner.classList.add('hidden');
        renderAuthPage();
    }
    
    function handleRenameChat() { /* ... same as before ... */ }
    function handleDeleteChat() { /* ... same as before ... */ }
    
    async function handleShareChat() {
        if (!appState.activeChatId) return;
        const result = await apiCall('/api/chat/share', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: appState.activeChatId }),
        });
        if (result.success) {
            const shareUrl = `${window.location.origin}/share/${result.share_id}`;
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'w-full p-2 bg-gray-700/50 rounded-lg border border-gray-600';
            input.value = shareUrl;
            input.readOnly = true;
            openModal('Shareable Link', input, () => {
                navigator.clipboard.writeText(shareUrl);
                showToast('Link copied to clipboard!', 'success');
            }, 'Copy Link');
        }
    }

    // --- UPGRADE & PAYMENT LOGIC ---
    async function renderUpgradePage() {
        const template = document.getElementById('template-upgrade-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        setupAppEventListeners();

        const plansContainer = document.getElementById('plans-container');
        const plansResult = await apiCall('/api/plans');
        if (!plansResult.success) {
            plansContainer.innerHTML = `<p class="text-red-400">Could not load plans.</p>`;
            return;
        }

        const { plans, user_plan, user_account_type } = plansResult;
        plansContainer.innerHTML = '';

        const planOrder = ['free', 'plus', 'pro', 'ultra'];
        planOrder.forEach(planId => {
            if (!plans[planId]) return;
            
            if (planId === 'plus' && user_account_type !== 'plus_user') return;
            if (['pro', 'ultra'].includes(planId) && user_account_type === 'plus_user') return;

            const plan = plans[planId];
            const card = document.createElement('div');
            const isCurrentUserPlan = planId === user_plan;
            
            card.className = `p-8 glassmorphism rounded-lg border-2 ${isCurrentUserPlan ? 'border-green-500' : 'border-gray-600'}`;
            card.innerHTML = `
                <h2 class="text-2xl font-bold text-center ${plan.color}">${plan.name}</h2>
                <p class="text-4xl font-bold text-center my-4 text-white">${plan.price_string}</p>
                <ul class="space-y-2 text-gray-300 mb-6">${plan.features.map(f => `<li>✓ ${f}</li>`).join('')}</ul>
                <button ${isCurrentUserPlan ? 'disabled' : ''} data-planid="${planId}" class="purchase-btn w-full mt-6 font-bold py-3 px-4 rounded-lg transition-opacity ${isCurrentUserPlan ? 'bg-gray-600 cursor-not-allowed' : 'bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90'}">
                    ${isCurrentUserPlan ? 'Current Plan' : 'Upgrade'}
                </button>
            `;
            plansContainer.appendChild(card);
        });

        plansContainer.querySelectorAll('.purchase-btn').forEach(btn => {
            if (!btn.disabled) {
                btn.onclick = () => handlePurchase(btn.dataset.planid);
            }
        });
    }

    async function handlePurchase(planId) {
        try {
            const config = await apiCall('/api/config');
            if (!config.success || !config.stripe_public_key) throw new Error("Could not retrieve payment configuration.");
            
            const stripe = Stripe(config.stripe_public_key);
            const sessionResult = await apiCall('/api/create-checkout-session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan_id: planId })
            });

            if (!sessionResult.success) throw new Error(sessionResult.error || "Could not create payment session.");
            
            const { error } = await stripe.redirectToCheckout({ sessionId: sessionResult.id });
            if (error) showToast(error.message, 'error');

        } catch (error) {
            showToast(error.message, 'error');
        }
    }

    // --- ADMIN LOGIC ---
    function renderAdminDashboard() {
        const template = document.getElementById('template-admin-dashboard');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('admin-logo-container');
        document.getElementById('admin-logout-btn').onclick = handleLogout;
        document.getElementById('announcement-form').onsubmit = handleSetAnnouncement;
        document.getElementById('admin-impersonate-btn').onclick = handleImpersonate;
        fetchAdminData();
    }

    async function fetchAdminData() {
        const data = await apiCall('/api/admin_data');
        if (!data.success) return;
        
        document.getElementById('admin-total-users').textContent = data.stats.total_users;
        document.getElementById('admin-pro-users').textContent = data.stats.pro_users;
        document.getElementById('admin-ultra-users').textContent = data.stats.ultra_users;
        document.getElementById('admin-plus-users').textContent = data.stats.plus_users;
        document.getElementById('announcement-input').value = data.announcement;

        const userList = document.getElementById('admin-user-list');
        userList.innerHTML = '';
        data.users.forEach(user => {
            const tr = document.createElement('tr');
            tr.className = 'border-b border-gray-700/50';
            tr.innerHTML = `
                <td class="p-2">${user.username}</td>
                <td class="p-2">${user.role}</td>
                <td class="p-2">${user.plan}</td>
                <td class="p-2">${user.account_type.replace('_', ' ')}</td>
                <td class="p-2 flex gap-2">
                    <button data-userid="${user.id}" class="delete-user-btn text-xs px-2 py-1 rounded bg-red-600">Delete</button>
                </td>`;
            userList.appendChild(tr);
        });
        userList.querySelectorAll('.delete-user-btn').forEach(btn => btn.onclick = handleAdminDeleteUser);
    }
    
    async function handleSetAnnouncement(e) {
        e.preventDefault();
        const text = document.getElementById('announcement-input').value;
        const result = await apiCall('/api/admin/announcement', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
        });
        if (result.success) {
            showToast(result.message, 'success');
            if (text) {
                DOMElements.announcementBanner.textContent = text;
                DOMElements.announcementBanner.classList.remove('hidden');
            } else {
                DOMElements.announcementBanner.classList.add('hidden');
            }
        }
    }

    function handleAdminDeleteUser(e) { /* ... same as before, but calls fetchAdminData() ... */ }
    
    async function handleImpersonate() {
        const username = prompt("Enter the username of the user to impersonate:");
        if (!username) return;
        const result = await apiCall('/api/admin/impersonate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username }),
        });
        if (result.success) {
            showToast(`Now impersonating ${username}. You will be logged in as them.`, 'success');
            setTimeout(() => window.location.reload(), 1500);
        }
    }


    // --- INITIAL LOAD ---
    checkLoginStatus();
});
</script>
</body>
</html>
"""

# --- 7. Backend Helper Functions ---
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


# --- 8. Core API Routes (Auth, Status) ---
@app.route('/')
def index():
    if 'impersonator_id' in session:
        impersonator = User.get(session['impersonator_id'])
        if impersonator:
            logout_user()
            login_user(impersonator)
            session.pop('impersonator_id', None)
            return redirect(url_for('index'))
    return Response(HTML_CONTENT, mimetype='text/html')

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
        new_user = User(id=username, username=username, password_hash=generate_password_hash(password), account_type=account_type)
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
            "chats": get_all_user_chats(user.id) if user.role != 'admin' else {},
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
            "chats": get_all_user_chats(current_user.id) if current_user.role != 'admin' else {},
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

    new_user = User(id=username, username=username, password_hash=generate_password_hash(password), role=role, plan='pro')
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({"success": True, "user": get_user_data_for_frontend(new_user)})


# --- 9. Chat API Routes ---
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
        
        # --- Context and Persona Injection ---
        current_time = datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')
        base_system_instruction = f"The current date and time is {current_time}. The user is located in Yorkton, Saskatchewan, Canada."
        
        if is_plus_mode and current_user.account_type == 'plus_user':
            persona_instruction = "You are MythAI Plus, a premium, enhanced AI assistant. Your goal is to provide faster, more detailed, and more insightful answers. You have access to advanced tools and knowledge. Be proactive and thorough."
        else:
            persona_instruction = "You are Myth AI, a powerful, general-purpose assistant for creative tasks, coding, and complex questions."
            
        final_system_instruction = f"{base_system_instruction}\n\n{persona_instruction}"

        # --- History and File Handling ---
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

        # --- Gemini API Call ---
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

# --- 10. Public Share and Payment Routes ---
@app.route('/share/<chat_id>')
def view_shared_chat(chat_id):
    chat = DB['chats'].get(chat_id)
    if not chat or not chat.get('is_public'):
        return "Chat not found or is not public.", 404
    
    # Simple, safe HTML rendering
    chat_html = f"<html><head><title>{chat['title']}</title></head><body><h1>{chat['title']}</h1>"
    for msg in chat['messages']:
        sender = "<b>You:</b>" if msg['sender'] == 'user' else "<b>Myth AI:</b>"
        # Basic escaping for content, though it's already sanitized by DOMPurify on the frontend
        content = msg['content'].replace('<', '&lt;').replace('>', '&gt;')
        chat_html += f"<p>{sender} {content.replace('/n', '<br>')}</p><hr>"
    chat_html += "</body></html>"
    return chat_html

@app.route('/api/plans')
@login_required
def get_plans():
    plans = {
        "free": {"name": "Free", "price_string": "Free", "features": ["15 Daily Messages", "Standard Model Access"], "color": "text-gray-300"},
        "pro": {"name": "Pro", "price_string": "$9.99 / month", "features": ["50 Daily Messages", "Image Uploads", "Priority Support"], "color": "text-indigo-400"},
        "ultra": {"name": "Ultra", "price_string": "$100 one-time", "features": ["Unlimited Messages", "Image Uploads", "Access to All Models"], "color": "text-purple-400"},
        "plus": {"name": "MythAI Plus", "price_string": "$4.99 / month", "features": ["100 Daily Messages", "Image Uploads", "Enhanced AI Persona"], "color": "text-cyan-400"}
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
            success_url=SITE_CONFIG["YOUR_DOMAIN"] + f'/payment-success?plan={plan_id}',
            cancel_url=SITE_CONFIG["YOUR_DOMAIN"] + '/payment-cancel',
            client_reference_id=current_user.id
        )
        return jsonify({'id': checkout_session.id})
    except Exception as e:
        logging.error(f"Stripe session creation failed for user {current_user.id}: {e}")
        return jsonify(error={'message': "Could not create payment session."}), 500

@app.route('/payment-success')
@login_required
def payment_success():
    plan = request.args.get('plan')
    if plan in ['pro', 'ultra', 'plus']:
        current_user.plan = plan
        save_database()
    return redirect('/?payment=success')

@app.route('/payment-cancel')
@login_required
def payment_cancel():
    return redirect('/?payment=cancel')


# --- 11. Admin Routes ---
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



