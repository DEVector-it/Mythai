import os
import json
import logging
import uuid
from flask import Flask, Response, request, stream_with_context, session, jsonify, redirect
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
import stripe
import random
from werkzeug.utils import secure_filename

# --- 1. Initial Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Gemini API Configuration ---
GEMINI_API_CONFIGURED = False
try:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.critical("FATAL ERROR: GEMINI_API_KEY environment variable not set.")
    else:
        genai.configure(api_key=api_key)
        GEMINI_API_CONFIGURED = True
except Exception as e:
    logging.critical(f"FATAL ERROR: Could not configure Gemini API. Details: {e}")

# --- Stripe API Configuration ---
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY')
YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN', 'http://localhost:5000')

# --- 2. Application Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-and-long-random-key-for-myth-ai-v7')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
SECRET_REGISTRATION_KEY = os.environ.get('SECRET_REGISTRATION_KEY', 'SUPER_SECRET_KEY_123')

# --- 3. Core AI Personas ---
MYTH_SYSTEM_PROMPT = """
You are Myth, a state-of-the-art, general-purpose AI assistant. You are knowledgeable, creative, and versatile. Your goal is to provide accurate, comprehensive, and helpful responses to a wide range of queries. You can write code, draft documents, brainstorm ideas, and answer complex questions. You are a powerful tool for productivity and creativity.
"""
STUDY_SYSTEM_PROMPT = """
You are Study Buddy, a friendly and encouraging academic assistant. Your primary goal is to help students understand subjects and learn effectively, not to do their work for them. You are a "study buddy" who guides them to the answers. Your core principles are:
1.  **NEVER give direct answers to homework questions.** If a user asks for a solution, you must refuse and instead guide them.
2.  **Guide the user.** Break down problems, ask leading questions, and explain underlying concepts.
3.  **Provide practice and examples.** Offer similar problems to help them learn.
4.  **Be motivational and positive.** Use encouraging language like "You've got this!" and "Let's tackle this together."
5.  **Keep it conversational.** Be an approachable and friendly study partner.
"""

# --- 4. Database Management ---
DB_FILE = 'database.json'
DB = {}

def load_database():
    """Loads the database from a JSON file into the global DB dictionary."""
    global DB
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'r') as f:
                data = json.load(f)
                DB = data
                # Re-instantiate User objects from the loaded dictionary data
                user_objects = {}
                for user_id, user_data in data.get('users', {}).items():
                    user = User(
                        id=user_data['id'], username=user_data['username'],
                        password_hash=user_data['password_hash'], role=user_data.get('role', 'user'),
                        plan=user_data.get('plan', 'free'), account_type=user_data.get('account_type', 'user'),
                        created_at=user_data.get('created_at', datetime.now().isoformat())
                    )
                    user.daily_messages = user_data.get('daily_messages', 0)
                    user.last_message_date = user_data.get('last_message_date', datetime.now().strftime("%Y-%m-%d"))
                    user_objects[user_id] = user
                DB['users'] = user_objects
                logging.info("Database loaded successfully from %s.", DB_FILE)
        else:
            initialize_default_db()
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Error loading database file: {e}. Initializing a default database.")
        initialize_default_db()

def save_database():
    """Saves the current state of the DB dictionary to a JSON file."""
    with app.app_context():
        # Create a serializable version of the database
        serializable_db = {
            'users': {uid: u.to_dict() for uid, u in DB.get('users', {}).items()},
            'chats': DB.get('chats', {}),
            'ads': DB.get('ads', {}),
            'site_settings': DB.get('site_settings', {})
        }
        with open(DB_FILE, 'w') as f:
            json.dump(serializable_db, f, indent=4)

def initialize_default_db():
    """Initializes the database with default values if it doesn't exist."""
    global DB
    DB = {"users": {}, "chats": {}, "ads": {}, "site_settings": {}}
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'adminadminnoob')
    admin = User(id='nameadmin', username='nameadmin', password_hash=generate_password_hash(admin_pass), role='admin', plan='ultra', account_type='user')
    DB['users']['nameadmin'] = admin
    advertiser_pass = 'adpass'
    advertiser = User(id='adminexample', username='adminexample', password_hash=generate_password_hash(advertiser_pass), role='advertiser', plan='pro', account_type='user')
    DB['users']['adminexample'] = advertiser
    DB['site_settings'] = {
        "announcement": "Welcome to Myth AI! Shareable links and user profiles are now live.",
        "plan_config": {
            "free": {"message_limit": 15, "model": "gemini-1.5-flash-latest", "price": 0},
            "pro": {"message_limit": 50, "model": "gemini-1.5-flash-latest", "price": 999},
            "ultra": {"message_limit": -1, "model": "gemini-1.5-pro-latest", "price": 10000}
        }
    }
    save_database()
    logging.info("Default database initialized and saved.")

# --- 5. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    """Handles unauthorized access attempts."""
    return jsonify({"error": "Login required.", "logged_in": False}), 401

class User(UserMixin):
    """User model for authentication and session management."""
    def __init__(self, id, username, password_hash, role='user', plan='free', account_type='user', created_at=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.plan = plan
        self.account_type = account_type
        self.daily_messages = 0
        self.last_message_date = datetime.now().strftime("%Y-%m-%d")
        self.created_at = created_at or datetime.now().isoformat()

    def to_dict(self):
        """Returns a dictionary representation of the user object for serialization."""
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def get(user_id):
        """Retrieves a user by their ID."""
        return DB.get('users', {}).get(user_id)

    @staticmethod
    def get_by_username(username):
        """Retrieves a user by their username."""
        for user in DB.get('users', {}).values():
            if user.username == username:
                return user
        return None

@login_manager.user_loader
def load_user(user_id):
    """Flask-Login user loader callback."""
    return User.get(user_id)

# --- 6. HTML, CSS, JavaScript Frontend ---
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Myth AI</title>
    <meta name="description" content="An advanced AI chat application with file analysis, user roles, and multiple chat modes.">
    <script src="https://js.stripe.com/v3/"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.2.12/marked.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/2.4.1/purify.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = { darkMode: 'class', theme: { extend: { fontFamily: { sans: ['Inter', 'sans-serif'], mono: ['Fira Code', 'monospace'] }, animation: { 'fade-in': 'fadeIn 0.5s ease-out forwards', 'scale-up': 'scaleUp 0.3s ease-out forwards' }, keyframes: { fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } }, scaleUp: { '0%': { transform: 'scale(0.95)', opacity: 0 }, '100%': { transform: 'scale(1)', opacity: 1 } } } } } }
    </script>
    <style>
        body { background-color: #111827; color: #e5e7eb; } ::-webkit-scrollbar { width: 8px; } ::-webkit-scrollbar-track { background: #1f2937; } ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 10px; }
        .glassmorphism { background: rgba(31, 41, 55, 0.5); backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
        .brand-gradient { background-image: linear-gradient(to right, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        pre { position: relative; } .copy-code-btn { position: absolute; top: 0.5rem; right: 0.5rem; background-color: #374151; color: white; border: none; padding: 0.25rem 0.5rem; border-radius: 0.25rem; opacity: 0; transition: opacity 0.2s; font-size: 0.75rem; } pre:hover .copy-code-btn { opacity: 1; }
        main.study-mode { background-image: linear-gradient(to top right, #4a3a0a, #1f2937); }
        .prose { color: #d1d5db; } .prose h1, .prose h2, .prose h3 { color: #fff; } .prose a { color: #60a5fa; } .prose code { color: #f97316; } .prose pre { background-color: #1e293b; }
    </style>
</head>
<body class="font-sans text-gray-200 antialiased">
    <div id="impersonation-banner" class="hidden text-center p-2 bg-yellow-600 text-black font-bold text-sm sticky top-0 z-50"></div>
    <div id="announcement-banner" class="hidden text-center p-2 bg-indigo-600 text-white text-sm sticky top-0 z-50"></div>
    <div id="app-container" class="relative h-screen w-screen"></div>
    <div id="modal-container"></div>
    <div id="toast-container" class="fixed top-6 right-6 z-[100] flex flex-col gap-2"></div>

    <!-- All UI templates are defined below -->
    <template id="template-logo"><svg width="48" height="48" viewBox="0 0 100 100"><defs><linearGradient id="logoGradient" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#3b82f6;" /><stop offset="100%" style="stop-color:#8b5cf6;" /></linearGradient></defs><path d="M50 10 C 27.9 10 10 27.9 10 50 C 10 72.1 27.9 90 50 90 C 72.1 90 90 72.1 90 50 C 90 27.9 72.1 10 50 10 Z M 50 15 C 69.3 15 85 30.7 85 50 C 85 69.3 69.3 85 50 85 C 30.7 85 15 69.3 15 50 C 15 30.7 30.7 15 50 15 Z" fill="url(#logoGradient)"/><path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></template>
    <template id="template-auth-page"><div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4"><div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up"><div class="flex justify-center mb-6" id="auth-logo-container"></div><h2 class="text-3xl font-bold text-center text-white mb-2" id="auth-title"></h2><p class="text-gray-400 text-center mb-8" id="auth-subtitle"></p><form id="auth-form"><div class="mb-4"><label for="username" class="block text-sm font-medium text-gray-300 mb-1">Username</label><input type="text" id="username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-amber-500" required></div><div class="mb-4"><label for="password" class="block text-sm font-medium text-gray-300 mb-1">Password</label><input type="password" id="password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-amber-500" required></div><div class="mb-6 hidden" id="account-type-container"><label for="account-type" class="block text-sm font-medium text-gray-300 mb-1">I am a...</label><select id="account-type" name="account_type" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600"><option value="user">General User</option><option value="student">Student</option></select></div><button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-amber-500 to-orange-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg"></button><p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p></form><div class="text-center mt-6"><button id="auth-toggle-btn" class="text-sm text-amber-400 hover:text-amber-300"></button></div></div></div></template>
    <template id="template-app-wrapper"><div class="flex h-full w-full"><aside id="sidebar" class="bg-gray-900/70 backdrop-blur-lg w-72 flex-shrink-0 flex flex-col p-2 h-full absolute md:relative z-20 transform -translate-x-full md:translate-x-0"><div class="flex-shrink-0 p-2 mb-2 flex items-center gap-3"><div id="app-logo-container"></div><h1 class="text-2xl font-bold brand-gradient">Myth AI</h1></div><div class="flex-shrink-0 p-2 space-y-2"><button id="new-myth-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> New Myth Chat</button><button id="new-study-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-amber-500/20 text-amber-400 hidden"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v15H6.5A2.5 2.5 0 0 1 4 14.5v-10A2.5 2.5 0 0 1 6.5 2z"/></svg> New Study Chat</button></div><div id="chat-history-list" class="flex-grow overflow-y-auto my-2 space-y-1 pr-1"></div><div class="flex-shrink-0 border-t border-gray-700 pt-2 space-y-1"><div id="user-info" class="p-3 text-sm cursor-pointer hover:bg-gray-700/50 rounded-lg"></div><button id="upgrade-plan-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-indigo-500/20 text-indigo-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 6v12m-6-6h12"/></svg> Upgrade Plan</button><button id="logout-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-red-500/20 text-red-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg> Logout</button></div></aside><main id="main-content" class="flex-1 flex flex-col bg-gray-800 h-full"><header class="flex-shrink-0 p-4 flex items-center justify-between border-b border-gray-700/50"><div class="flex items-center gap-2"><button id="menu-toggle-btn" class="p-2 rounded-lg hover:bg-gray-700/50 md:hidden"><svg width="24" height="24" viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg></button><h2 id="chat-title" class="text-xl font-semibold truncate"></h2></div><div class="flex items-center gap-4"><button id="share-chat-btn" title="Share Chat" class="p-2 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg></button><button id="rename-chat-btn" title="Rename Chat" class="p-2 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button><button id="delete-chat-btn" title="Delete Chat" class="p-2 rounded-lg hover:bg-red-500/20 text-red-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m-6 4v6m4 4v-6"/></svg></button></div></header><div id="chat-window" class="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 min-h-0"></div><div class="flex-shrink-0 p-2 md:p-4 md:px-6 border-t border-gray-700/50"><div class="max-w-4xl mx-auto"><div id="stop-generating-container" class="text-center mb-2" style="display: none;"><button id="stop-generating-btn" class="bg-red-600/50 text-white font-semibold py-2 px-4 rounded-lg flex items-center gap-2 mx-auto"><svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><rect width="10" height="10" x="3" y="3" rx="1"/></svg> Stop</button></div><div id="attachment-preview" class="hidden items-center gap-2 text-sm bg-gray-700/50 p-2 rounded-md mb-2"><span id="attachment-filename"></span><button id="remove-attachment-btn" class="text-red-400">&times;</button></div><div class="relative glassmorphism rounded-2xl shadow-lg"><textarea id="user-input" placeholder="Message Myth AI..." class="w-full bg-transparent p-4 pr-24 resize-none rounded-2xl focus:outline-none focus:ring-2 focus:ring-blue-500" rows="1"></textarea><div class="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-2"><button id="attach-file-btn" title="Attach File (Pro/Ultra)" class="p-2 rounded-full hover:bg-gray-600/50 text-gray-400 disabled:text-gray-600"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button><input type="file" id="file-input" class="hidden"/><button id="send-btn" class="p-2 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 disabled:from-gray-500"><svg width="20" height="20" fill="white" viewBox="0 0 24 24"><path d="M2 22l20-10L2 2z"/></svg></button></div></div><div class="text-xs text-gray-400 mt-2 text-center" id="message-limit-display"></div></div></div></main></div></template>
    <template id="template-welcome-screen"><div class="flex flex-col items-center justify-center h-full text-center p-4"><div class="w-24 h-24 mb-6" id="welcome-logo-container"></div><h2 class="text-3xl md:text-4xl font-bold mb-4">Welcome to Myth AI</h2><p class="text-gray-400 max-w-md">Your personal AI assistant. Start a new chat from the sidebar to begin.</p></div></template>
    <template id="template-modal"><div class="modal-backdrop fixed inset-0 bg-black/60"></div><div class="modal-content fixed inset-0 flex items-center justify-center p-4"><div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl relative"><button class="close-modal-btn absolute top-4 right-4 text-gray-400 hover:text-white text-3xl">&times;</button><h3 id="modal-title" class="text-2xl font-bold text-center mb-4"></h3><div id="modal-body" class="text-center text-gray-300"></div></div></div></template>
    <template id="template-upgrade-page"><div class="w-full h-full bg-gray-900 p-4 sm:p-6 overflow-y-auto"><header class="flex justify-between items-center mb-8"><h1 class="text-3xl font-bold brand-gradient">Upgrade Your Plan</h1><button id="back-to-chat-btn" class="bg-gray-700 hover:bg-gray-600 text-white font-bold py-2 px-4 rounded-lg">Back to Chat</button></header><div class="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-6xl mx-auto"><div class="p-8 glassmorphism rounded-lg border-2 border-gray-600 flex flex-col"><h2 class="text-2xl font-bold text-center">Free Plan</h2><p class="text-4xl font-bold text-center my-4 text-white">Free</p><ul class="space-y-2 text-gray-400 flex-grow"><li>✓ 15 Daily Messages</li><li>✓ Standard Model Access</li><li>✓ Community Support</li></ul><button class="w-full mt-6 bg-gray-600 text-white font-bold py-3 px-4 rounded-lg cursor-not-allowed">Current Plan</button></div><div class="p-8 glassmorphism rounded-lg border-2 border-indigo-500 flex flex-col"><h2 class="text-2xl font-bold text-center text-indigo-400">Pro Plan</h2><p class="text-4xl font-bold text-center my-4 text-white">$9.99 <span class="text-lg font-normal text-gray-400">/ month</span></p><ul class="space-y-2 text-gray-300 flex-grow"><li>✓ 50 Daily Messages</li><li>✓ Priority Support</li><li>✓ File Upload & Analysis</li></ul><button data-plan="pro" class="purchase-btn w-full mt-6 bg-gradient-to-r from-blue-600 to-indigo-600 text-white font-bold py-3 px-4 rounded-lg">Upgrade to Pro</button></div><div class="p-8 glassmorphism rounded-lg border-2 border-amber-500 flex flex-col"><h2 class="text-2xl font-bold text-center text-amber-400">Ultra Plan</h2><p class="text-4xl font-bold text-center my-4 text-white">$100 <span class="text-lg font-normal text-gray-400">/ one-time</span></p><ul class="space-y-2 text-gray-300 flex-grow"><li>✓ **Unlimited** Messages</li><li>✓ **Highest-Tier AI Model**</li><li>✓ All Pro Features</li><li>✓ Admin Impersonation (if admin)</li></ul><button data-plan="ultra" class="purchase-btn w-full mt-6 bg-gradient-to-r from-amber-500 to-orange-600 text-white font-bold py-3 px-4 rounded-lg">Go Ultra</button></div></div></div></template>
    <template id="template-admin-dashboard"><!-- Admin Panel HTML --></template>
    <template id="template-ad-dashboard"><!-- Advertiser Panel HTML --></template>
    <template id="template-user-settings-page"><!-- User Settings HTML --></template>
    
<script>
// --- JAVASCRIPT FRONTEND LOGIC (Myth AI v7.0) ---
document.addEventListener('DOMContentLoaded', () => {
    // ... (Full JavaScript implementation here)
});
</script>
</body>
</html>
"""

# --- 7. Backend Logic (Flask Routes) ---

def get_plan_config():
    """Retrieves the current plan configuration from the database."""
    return DB.get('site_settings', {}).get('plan_config', {})

@app.route('/')
def index_route():
    """Serves the main application shell."""
    return HTML_CONTENT

@app.route('/shared/<share_id>')
def shared_chat_route(share_id):
    """Serves the read-only view for a shared chat."""
    for chat in DB.get('chats', {}).values():
        if chat.get('share_id') == share_id and chat.get('is_public'):
            return f"<h1>Shared Chat: {chat['title']}</h1>" + "".join([f"<p><b>{msg['sender']}:</b> {msg['content']}</p>" for msg in chat['messages']])
    return "Shared chat not found or is private.", 404

# --- API: User Auth & Profile ---
@app.route('/api/signup', methods=['POST'])
def signup_route():
    data = request.get_json()
    username, password, account_type = data.get('username'), data.get('password'), data.get('account_type', 'user')
    if not username or not password: return jsonify({"error": "Missing required fields."}), 400
    if User.get_by_username(username): return jsonify({"error": "Username already exists."}), 409
    
    new_user = User(id=username, username=username, password_hash=generate_password_hash(password), account_type=account_type)
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({"success": True, "user": get_user_data_for_frontend(new_user), "chats": {}, "settings": DB['site_settings']})

@app.route('/api/login', methods=['POST'])
def login_route():
    data = request.get_json()
    user = User.get_by_username(data.get('username'))
    if user and check_password_hash(user.password_hash, data.get('password')):
        login_user(user, remember=True)
        return jsonify({"success": True, "user": get_user_data_for_frontend(user), "chats": get_all_user_chats(user.id), "settings": DB['site_settings']})
    return jsonify({"error": "Invalid username or password."}), 401

@app.route('/api/logout')
def logout_route():
    logout_user()
    session.pop('admin_user_id', None) # Clear impersonation on logout
    return jsonify({"success": True})

@app.route('/api/status')
def status_route():
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True,
            "user": get_user_data_for_frontend(current_user),
            "chats": get_all_user_chats(current_user.id),
            "settings": DB['site_settings']
        })
    return jsonify({"logged_in": False})

# --- API: Core Chat Functionality ---
@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api_route():
    if not GEMINI_API_CONFIGURED: return jsonify({"error": "API not configured."}), 503
    
    form_data = request.form
    chat_id, prompt = form_data.get('chat_id'), form_data.get('prompt')
    file = request.files.get('file')
    
    chat = DB['chats'].get(chat_id)
    if not chat or chat.get('user_id') != current_user.id: return jsonify({"error": "Chat not found."}), 404

    plan_config = get_plan_config()
    plan_details = plan_config.get(current_user.plan, plan_config['free'])
    
    if plan_details['message_limit'] != -1 and current_user.daily_messages >= plan_details["message_limit"]:
        return jsonify({"error": f"Daily message limit reached."}), 429
    if file and current_user.plan not in ['pro', 'ultra']:
        return jsonify({"error": "File upload requires a Pro or Ultra plan."}), 403

    chat_mode = chat.get('mode', 'myth')
    system_instruction = STUDY_SYSTEM_PROMPT if chat_mode == 'study' else MYTH_SYSTEM_PROMPT
    model_name = plan_details['model']
    
    history = [{"role": ('model' if msg['sender'] == 'model' else 'user'), "parts": [msg['content']]} for msg in chat['messages']]
    model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
    chat_session = model.start_chat(history=history)
    
    message_parts = [prompt]
    file_info = None
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        uploaded_file = genai.upload_file(path=filepath)
        message_parts.append(uploaded_file)
        file_info = filename

    chat['messages'].append({'sender': 'user', 'content': prompt, 'file': file_info})

    def generate_chunks():
        try:
            response_stream = chat_session.send_message(message_parts, stream=True)
            full_response_text = ""
            for chunk in response_stream:
                if chunk.text:
                    full_response_text += chunk.text
                    yield chunk.text
            
            if full_response_text.strip():
                chat['messages'].append({'sender': 'model', 'content': full_response_text})
                current_user.daily_messages += 1
                if len(chat['messages']) < 4:
                    title_response = genai.GenerativeModel('gemini-1.5-flash-latest').generate_content(f"Summarize with a short title (4 words max): User: \"{prompt}\"")
                    chat['title'] = title_response.text.strip().replace('"', '') or "New Conversation"
                save_database()
        except Exception as e:
            logging.error(f"Gemini stream error: {e}")
            yield json.dumps({"error": f"AI model error: {str(e)}"})

    return Response(stream_with_context(generate_chunks()), mimetype='text/plain')

# ... Other routes are implemented here in the full version ...

# --- Helper Functions ---
def get_user_data_for_frontend(user):
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_config = get_plan_config()
    plan_details = plan_config.get(user.plan, plan_config['free'])
    return {
        "id": user.id, "username": user.username, "role": user.role, "plan": user.plan,
        "account_type": user.account_type, "daily_messages": user.daily_messages,
        "message_limit": plan_details.get("message_limit", 15),
        "is_impersonating": 'admin_user_id' in session
    }

def check_and_reset_daily_limit(user):
    if not isinstance(user, User): return
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0
        save_database()

# --- Application Startup ---
if __name__ == '__main__':
    load_database()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)




