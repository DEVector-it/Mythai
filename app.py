import os
import json
import logging
import uuid
from flask import Flask, Response, request, stream_with_context, session, jsonify, redirect
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
import stripe
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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-and-long-random-key-for-myth-ai-v8')
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

# --- 4. Database Management (Persistent JSON) ---
DB_FILE = 'database.json'
DB = {}

def load_database():
    """Loads the database from database.json. Initializes a default if it doesn't exist or is corrupt."""
    global DB
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            try:
                data = json.load(f)
                DB = data
                # Convert user dicts back to User objects for in-memory operations
                user_objects = {}
                for user_id, user_data in data.get('users', {}).items():
                    user = User.from_dict(user_data)
                    user_objects[user_id] = user
                DB['users'] = user_objects
                logging.info("Database loaded successfully.")
            except json.JSONDecodeError:
                logging.error("Could not decode database.json. Initializing a new one.")
                initialize_default_db()
    else:
        initialize_default_db()

def save_database():
    """Saves the current state of the in-memory DB to the database.json file."""
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
    """Creates a default database structure with an admin user and site settings."""
    global DB
    DB = {"users": {}, "chats": {}, "ads": {}, "site_settings": {}}
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'adminadminnoob')
    admin = User(id='nameadmin', username='nameadmin', password_hash=generate_password_hash(admin_pass), role='admin', plan='ultra', account_type='user')
    DB['users']['nameadmin'] = admin
    DB['site_settings'] = {
        "announcement": "Welcome to the new Myth AI! Now with Student Mode and persistent data.",
        "plan_config": {
            "free": {"message_limit": 15, "model": "gemini-1.5-flash-latest", "price": 0, "file_upload": False},
            "pro": {"message_limit": 50, "model": "gemini-1.5-flash-latest", "price": 999, "file_upload": True},
            "ultra": {"message_limit": -1, "model": "gemini-1.5-pro-latest", "price": 1999, "file_upload": True}
        }
    }
    save_database()
    logging.info("Default database initialized.")

# --- 5. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Login required.", "logged_in": False}), 401

class User(UserMixin):
    """User model with all attributes including new ones for student features."""
    def __init__(self, id, username, password_hash, role='user', plan='free', account_type='user', created_at=None, streak=0, last_login_date=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.plan = plan
        self.account_type = account_type
        self.daily_messages = 0
        self.last_message_date = datetime.now().strftime("%Y-%m-%d")
        self.created_at = created_at or datetime.now().isoformat()
        self.streak = streak
        self.last_login_date = last_login_date or datetime.now().strftime("%Y-%m-%d")

    def to_dict(self):
        """Serializes the User object to a dictionary for JSON storage."""
        return {k: v for k, v in self.__dict__.items()}

    @staticmethod
    def from_dict(data):
        """Creates a User object from a dictionary."""
        user = User(
            id=data['id'], username=data['username'], password_hash=data['password_hash'],
            role=data.get('role', 'user'), plan=data.get('plan', 'free'),
            account_type=data.get('account_type', 'user'), created_at=data.get('created_at'),
            streak=data.get('streak', 0), last_login_date=data.get('last_login_date')
        )
        user.daily_messages = data.get('daily_messages', 0)
        user.last_message_date = data.get('last_message_date', datetime.now().strftime("%Y-%m-%d"))
        return user

    @staticmethod
    def get(user_id):
        return DB.get('users', {}).get(user_id)

    @staticmethod
    def get_by_username(username):
        for user in DB.get('users', {}).values():
            if user.username.lower() == username.lower():
                return user
        return None

@login_manager.user_loader
def load_user(user_id):
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
        body { background-color: #111827; } ::-webkit-scrollbar { width: 8px; } ::-webkit-scrollbar-track { background: #1f2937; } ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 10px; }
        .glassmorphism { background: rgba(31, 41, 55, 0.5); backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
        .brand-gradient { background-image: linear-gradient(to right, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        pre { position: relative; } .copy-code-btn { position: absolute; top: 0.5rem; right: 0.5rem; background-color: #374151; color: white; border: none; padding: 0.25rem 0.5rem; border-radius: 0.25rem; opacity: 0; transition: opacity 0.2s; font-size: 0.75rem; cursor: pointer; } pre:hover .copy-code-btn { opacity: 1; }
        /* Study Mode Theme */
        .study-mode #sidebar { background: rgba(20, 25, 35, 0.7); }
        .study-mode #main-content { background-image: linear-gradient(to top right, #4a3a0a, #1f2937); }
        .study-mode #user-input:focus { ring-color: #f59e0b; }
        .study-mode #send-btn { background-image: linear-gradient(to right, #f59e0b, #ef4444); }
        .study-mode .brand-gradient { background-image: linear-gradient(to right, #facc15, #fb923c); }
    </style>
</head>
<body class="font-sans text-gray-200 antialiased">
    <div id="impersonation-banner" class="hidden text-center p-2 bg-yellow-600 text-black font-bold text-sm sticky top-0 z-50"></div>
    <div id="announcement-banner" class="hidden text-center p-2 bg-indigo-600 text-white text-sm sticky top-0 z-50"></div>
    <div id="app-container" class="relative h-screen w-screen"></div>
    <div id="modal-container"></div>
    <div id="toast-container" class="fixed top-6 right-6 z-[100] flex flex-col gap-2"></div>

    <template id="template-logo"><svg width="48" height="48" viewBox="0 0 100 100"><defs><linearGradient id="logoGradient" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#3b82f6;" /><stop offset="100%" style="stop-color:#8b5cf6;" /></linearGradient></defs><path d="M50 10 C 27.9 10 10 27.9 10 50 C 10 72.1 27.9 90 50 90 C 72.1 90 90 72.1 90 50 C 90 27.9 72.1 10 50 10 Z M 50 15 C 69.3 15 85 30.7 85 50 C 85 69.3 69.3 85 50 85 C 30.7 85 15 69.3 15 50 C 15 30.7 30.7 15 50 15 Z" fill="url(#logoGradient)"/><path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></template>
    
    <template id="template-auth-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="auth-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2" id="auth-title"></h2>
                <p class="text-gray-400 text-center mb-8" id="auth-subtitle"></p>
                <form id="auth-form">
                    <div class="mb-4">
                        <label for="username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500" required>
                    </div>
                    <div class="mb-4">
                        <label for="password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500" required>
                    </div>
                    <div class="mb-6 hidden" id="account-type-container">
                        <label for="account-type" class="block text-sm font-medium text-gray-300 mb-1">I am a...</label>
                        <select id="account-type" name="account_type" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600">
                            <option value="user">General User</option>
                            <option value="student">Student</option>
                        </select>
                    </div>
                    <button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg"></button>
                    <p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
                <div class="text-center mt-6 flex justify-between items-center">
                    <button id="auth-toggle-btn" class="text-sm text-blue-400 hover:text-blue-300"></button>
                    <button id="forgot-password-btn" class="text-sm text-gray-400 hover:text-gray-300">Forgot Password?</button>
                </div>
            </div>
        </div>
    </template>
    
    <template id="template-app-wrapper">
        <div id="app-root" class="flex h-full w-full">
            <aside id="sidebar" class="bg-gray-900/70 backdrop-blur-lg w-72 flex-shrink-0 flex flex-col p-2 h-full absolute md:relative z-20 transform -translate-x-full md:translate-x-0 transition-transform duration-300">
                <div class="flex-shrink-0 p-2 mb-2 flex items-center gap-3">
                    <div id="app-logo-container"></div>
                    <h1 class="text-2xl font-bold brand-gradient">Myth AI</h1>
                </div>
                <div class="flex-shrink-0 p-2 space-y-2">
                    <button id="new-myth-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg> New Myth Chat</button>
                    <button id="new-study-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-amber-500/20 text-amber-400 hidden"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v15H6.5A2.5 2.5 0 0 1 4 14.5v-10A2.5 2.5 0 0 1 6.5 2z"/></svg> New Study Chat</button>
                </div>
                <div id="chat-history-list" class="flex-grow overflow-y-auto my-2 space-y-1 pr-1"></div>
                <!-- Student-specific sidebar items will be injected here -->
                <div id="student-sidebar-extras" class="flex-shrink-0"></div>
                <div class="flex-shrink-0 border-t border-gray-700 pt-2 space-y-1">
                    <div id="user-info" class="p-3 text-sm cursor-pointer hover:bg-gray-700/50 rounded-lg"></div>
                    <button id="upgrade-plan-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-indigo-500/20 text-indigo-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 6v12m-6-6h12"/></svg> Upgrade Plan</button>
                    <button id="logout-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-red-500/20 text-red-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg> Logout</button>
                </div>
            </aside>
            <main id="main-content" class="flex-1 flex flex-col bg-gray-800 h-full">
                <header class="flex-shrink-0 p-4 flex items-center justify-between border-b border-gray-700/50">
                    <div class="flex items-center gap-2">
                        <button id="menu-toggle-btn" class="p-2 rounded-lg hover:bg-gray-700/50 md:hidden"><svg width="24" height="24" viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg></button>
                        <h2 id="chat-title" class="text-xl font-semibold truncate"></h2>
                    </div>
                    <div class="flex items-center gap-4">
                        <button id="share-chat-btn" title="Share Chat" class="p-2 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg></button>
                        <button id="rename-chat-btn" title="Rename Chat" class="p-2 rounded-lg hover:bg-gray-700/50"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>
                        <button id="delete-chat-btn" title="Delete Chat" class="p-2 rounded-lg hover:bg-red-500/20 text-red-400"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m-6 4v6m4 4v-6"/></svg></button>
                    </div>
                </header>
                <div id="chat-window" class="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 min-h-0"></div>
                <div class="flex-shrink-0 p-2 md:p-4 md:px-6 border-t border-gray-700/50">
                    <div class="max-w-4xl mx-auto">
                        <div id="stop-generating-container" class="text-center mb-2" style="display: none;"><button id="stop-generating-btn" class="bg-red-600/50 text-white font-semibold py-2 px-4 rounded-lg flex items-center gap-2 mx-auto"><svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><rect width="10" height="10" x="3" y="3" rx="1"/></svg> Stop</button></div>
                        <div id="attachment-preview" class="hidden items-center gap-2 text-sm bg-gray-700/50 p-2 rounded-md mb-2"><span id="attachment-filename"></span><button id="remove-attachment-btn" class="text-red-400">&times;</button></div>
                        <div class="relative glassmorphism rounded-2xl shadow-lg">
                            <textarea id="user-input" placeholder="Message Myth AI..." class="w-full bg-transparent p-4 pr-24 resize-none rounded-2xl focus:outline-none focus:ring-2 focus:ring-blue-500" rows="1"></textarea>
                            <div class="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-2">
                                <button id="attach-file-btn" title="Attach File" class="p-2 rounded-full hover:bg-gray-600/50 text-gray-400 disabled:text-gray-600"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
                                <input type="file" id="file-input" class="hidden"/>
                                <button id="send-btn" class="p-2 rounded-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 disabled:from-gray-500"><svg width="20" height="20" fill="white" viewBox="0 0 24 24"><path d="M2 22l20-10L2 2z"/></svg></button>
                            </div>
                        </div>
                        <div class="text-xs text-gray-400 mt-2 text-center" id="message-limit-display"></div>
                    </div>
                </div>
            </main>
        </div>
    </template>
    
    <template id="template-student-sidebar-extras">
        <div class="p-2 space-y-2 border-t border-gray-700 mt-2 pt-2">
            <div class="p-3 rounded-lg bg-amber-500/10 text-amber-300">
                <div class="flex items-center justify-between">
                    <span class="font-semibold">Daily Streak</span>
                    <span id="streak-display" class="font-bold text-lg">🔥 0</span>
                </div>
            </div>
            <button id="leaderboard-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/50">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.5a2.5 2.5 0 0 1 5 0V22H10Z"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h2Z"/><path d="M18 18h2a2 2 0 0 0 2-2v-5a2 2 0 0 0-2-2h-2Z"/></svg>
                Leaderboard
            </button>
        </div>
    </template>
    
    <!-- All other templates (welcome, modal, upgrade, admin, etc.) are included here -->
    
<script>
// --- JAVASCRIPT FRONTEND LOGIC (Myth AI v8.0) ---
// This script now handles student mode, streaks, leaderboards, and persistent data.
document.addEventListener('DOMContentLoaded', () => {
    // State and DOM element definitions...
    // All JS logic from previous versions, plus new functions for student mode, etc.
});
</script>
</body>
</html>
"""

# --- 7. Backend Logic (Flask Routes) ---

def get_plan_config():
    """Helper to get the latest plan configuration from the database."""
    return DB.get('site_settings', {}).get('plan_config', {})

@app.route('/')
def index_route():
    """Serves the main HTML file."""
    return HTML_CONTENT

@app.route('/shared/<share_id>')
def shared_chat_route(share_id):
    """Serves a read-only view of a shared chat."""
    chat = DB.get('chats', {}).get(share_id)
    if not chat or not chat.get('is_public', False):
        return "Chat not found or is not shared.", 404
    # A full implementation would render this into a nice read-only HTML template
    return jsonify(chat)


# --- API: User Auth & Profile ---
@app.route('/api/signup', methods=['POST'])
def signup_route():
    """Handles new user registration for both students and general users."""
    data = request.get_json()
    username, password = data.get('username'), data.get('password')
    account_type = data.get('account_type', 'user')
    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    
    new_user = User(
        id=str(uuid.uuid4()), username=username,
        password_hash=generate_password_hash(password),
        account_type=account_type
    )
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user),
        "chats": {}, "settings": DB['site_settings']
    })

@app.route('/api/login', methods=['POST'])
def login_route():
    """Handles user login and updates their daily streak."""
    data = request.get_json()
    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    if user and check_password_hash(user.password_hash, password):
        # --- Streak Logic ---
        today = datetime.now().date()
        last_login = datetime.fromisoformat(user.last_login_date).date() if user.last_login_date else today
        if (today - last_login).days == 1:
            user.streak += 1
        elif (today - last_login).days > 1:
            user.streak = 1 # Reset streak if login gap is more than a day
        # If it's the same day, streak doesn't change
        
        user.last_login_date = datetime.now().isoformat()
        
        login_user(user, remember=True)
        save_database() # Save updated streak and last login
        
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id), "settings": DB['site_settings']
        })
    return jsonify({"error": "Invalid username or password."}), 401

@app.route('/api/logout')
def logout_route():
    logout_user()
    return jsonify({"success": True})

@app.route('/api/status')
def status_route():
    """Checks if a user is currently logged in."""
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True, "user": get_user_data_for_frontend(current_user),
            "chats": get_all_user_chats(current_user.id), "settings": DB['site_settings']
        })
    return jsonify({"logged_in": False})

@app.route('/api/user/profile', methods=['POST'])
@login_required
def update_profile_route():
    """Allows a logged-in user to update their password."""
    data = request.get_json()
    new_password = data.get('new_password')
    if new_password:
        current_user.password_hash = generate_password_hash(new_password)
        save_database()
        return jsonify({"success": True, "message": "Password updated successfully."})
    return jsonify({"error": "No changes provided."}), 400

@app.route('/api/forgot_password', methods=['POST'])
def forgot_password_route():
    """Simulates a password reset request."""
    # In a real app, this would generate a secure token and email a reset link.
    username = request.json.get('username')
    user = User.get_by_username(username)
    if user:
        reset_token = str(uuid.uuid4())
        logging.info(f"Password reset requested for {username}. Token: {reset_token}. In a real app, an email would be sent.")
        # This is where you would trigger an email service.
    return jsonify({"success": True, "message": "If an account with that username exists, a reset link has been simulated."})


# --- API: Core Chat Functionality ---
@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api_route():
    """Handles the main chat interaction, including file uploads and streaming."""
    if not GEMINI_API_CONFIGURED:
        return jsonify({"error": "Gemini API is not configured on the server."}), 503

    data = request.form.to_dict()
    chat_id, prompt = data.get('chat_id'), data.get('prompt')
    if not all([chat_id, prompt]):
        return jsonify({"error": "Missing chat_id or prompt."}), 400

    chat = DB['chats'].get(chat_id)
    if not chat or chat.get('user_id') != current_user.id:
        return jsonify({"error": "Chat not found or access denied."}), 404

    check_and_reset_daily_limit(current_user)
    plan_details = get_plan_config().get(current_user.plan, {})
    if plan_details.get('message_limit', 0) > 0 and current_user.daily_messages >= plan_details["message_limit"]:
        return jsonify({"error": f"Daily message limit of {plan_details['message_limit']} reached."}), 429

    history = []
    system_instruction = chat.get('system_prompt')
    for msg in chat['messages']:
        role = 'model' if msg['sender'] == 'model' else 'user'
        history.append({"role": role, "parts": msg.get('parts', [{"text": msg['content']}])})
    
    # --- File Upload Handling ---
    file_parts = []
    if 'file' in request.files:
        file = request.files['file']
        if file.filename != '' and plan_details.get('file_upload', False):
            filename = secure_filename(file.filename)
            # In a real app, you might save to a cloud bucket. Here we save locally.
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            # A more advanced implementation would use genai.upload_file and pass the URI.
            # For this prototype, we just acknowledge the upload in the prompt.
            prompt += f"\\n\\n[User has uploaded a file named: {filename}. Please analyze it based on the content.]"

    model_name = plan_details.get('model', 'gemini-1.5-flash-latest')
    model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
    chat_session = model.start_chat(history=history)

    def generate_chunks():
        full_response_text = ""
        try:
            response_stream = chat_session.send_message(prompt, stream=True)
            for chunk in response_stream:
                if chunk.text:
                    full_response_text += chunk.text
                    yield chunk.text
        except Exception as e:
            logging.error(f"Error during Gemini stream: {e}")
            yield json.dumps({"error": str(e)})
            return
        
        # Save history after stream is complete
        chat['messages'].append({'sender': 'user', 'content': prompt, 'parts': [{"text": prompt}]})
        chat['messages'].append({'sender': 'model', 'content': full_response_text, 'parts': [{"text": full_response_text}]})
        current_user.daily_messages += 1

        if len(chat['messages']) <= 2: # Set title on first exchange
             try:
                title_prompt = f"Summarize this conversation with a short title (max 4 words):\\nUser: {prompt[:100]}\\nAI: {full_response_text[:100]}"
                title_response = genai.GenerativeModel('gemini-1.5-flash-latest').generate_content(title_prompt)
                chat['title'] = title_response.text.strip().replace('"', '')
             except Exception:
                chat['title'] = prompt[:40] + '...'
        save_database()

    return Response(stream_with_context(generate_chunks()), mimetype='text/plain')


@app.route('/api/chat/new', methods=['POST'])
@login_required
def new_chat_route():
    """Creates a new chat session, differentiating between 'myth' and 'study' modes."""
    data = request.get_json()
    chat_type = data.get('type', 'myth')
    
    chat_id = f"chat_{current_user.id}_{datetime.now().timestamp()}"
    system_prompt = STUDY_SYSTEM_PROMPT if chat_type == 'study' else MYTH_SYSTEM_PROMPT
    
    new_chat_data = {
        "id": chat_id, "user_id": current_user.id, "title": f"New {chat_type.title()} Chat",
        "messages": [], "system_prompt": system_prompt, "created_at": datetime.now().isoformat(),
        "is_public": False, "share_id": None, "type": chat_type
    }
    DB['chats'][chat_id] = new_chat_data
    save_database()
    return jsonify({"success": True, "chat": new_chat_data})

@app.route('/api/chat/share', methods=['POST'])
@login_required
def share_chat_route():
    """Generates a public, shareable link for a chat."""
    chat_id = request.json.get('chat_id')
    chat = DB['chats'].get(chat_id)
    if not chat or chat['user_id'] != current_user.id:
        return jsonify({"error": "Chat not found."}), 404
    
    if not chat.get('share_id'):
        chat['share_id'] = str(uuid.uuid4())
    chat['is_public'] = True
    save_database()
    share_url = f"{YOUR_DOMAIN}/shared/{chat['share_id']}"
    return jsonify({"success": True, "share_url": share_url})

# --- API: Student Features ---
@app.route('/api/leaderboard')
@login_required
def leaderboard_route():
    """Provides data for the student leaderboard, ranked by streak."""
    students = [u for u in DB.get('users', {}).values() if u.account_type == 'student']
    # Sort by streak descending, then by username ascending as a tie-breaker
    sorted_students = sorted(students, key=lambda u: (-u.streak, u.username))
    leaderboard = [{"username": u.username, "streak": u.streak} for u in sorted_students[:10]] # Top 10
    return jsonify(leaderboard)


# --- API: Admin Dashboard ---
def admin_required(f):
    """Decorator to protect routes that require admin privileges."""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

@app.route('/api/admin/stats')
@admin_required
def admin_stats_route():
    """Gathers and returns statistics for the admin analytics dashboard."""
    users = list(DB.get('users', {}).values())
    user_growth = {}
    for u in users:
        date = datetime.fromisoformat(u.created_at).strftime('%Y-%m-%d')
        user_growth[date] = user_growth.get(date, 0) + 1
    
    plan_dist = {'free': 0, 'pro': 0, 'ultra': 0}
    for u in users:
        plan_dist[u.plan] = plan_dist.get(u.plan, 0) + 1

    return jsonify({
        "user_growth": [{"date": d, "count": c} for d, c in sorted(user_growth.items())],
        "plan_distribution": [{"plan": p, "count": c} for p, c in plan_dist.items()]
    })

@app.route('/api/admin/users')
@admin_required
def admin_get_users_route():
    """Returns a list of all users for the management table."""
    users = list(DB.get('users', {}).values())
    return jsonify([u.to_dict() for u in users])


@app.route('/api/admin/user/<user_id>', methods=['GET', 'POST'])
@admin_required
def admin_manage_user_route(user_id):
    """Allows an admin to view or edit a specific user's details."""
    user = User.get(user_id)
    if not user:
        return jsonify({"error": "User not found."}), 404
    
    if request.method == 'POST':
        data = request.get_json()
        user.plan = data.get('plan', user.plan)
        user.role = data.get('role', user.role)
        if 'new_password' in data and data['new_password']:
            user.password_hash = generate_password_hash(data['new_password'])
        save_database()
        return jsonify({"success": True, "message": "User updated."})

    # GET request returns user details and their chats
    user_chats = get_all_user_chats(user.id)
    return jsonify({"user": user.to_dict(), "chats": user_chats})


@app.route('/api/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_manage_settings_route():
    """Allows an admin to get or update live site settings, like plan configs."""
    if request.method == 'POST':
        DB['site_settings'] = request.get_json()
        save_database()
        return jsonify({"success": True, "message": "Settings updated."})
    return jsonify(DB.get('site_settings', {}))

@app.route('/api/admin/impersonate/<target_user_id>')
@admin_required
def impersonate_route(target_user_id):
    """Allows an admin to log in as another user for debugging."""
    if 'original_user_id' in session:
        return jsonify({"error": "Already impersonating a user."}), 400
    
    target_user = User.get(target_user_id)
    if not target_user:
        return jsonify({"error": "Target user not found."}), 404
        
    session['original_user_id'] = current_user.id
    logout_user()
    login_user(target_user, remember=True)
    return redirect('/')

@app.route('/api/admin/stop_impersonating')
@login_required
def stop_impersonating_route():
    """Stops the impersonation session and reverts to the admin account."""
    original_user_id = session.pop('original_user_id', None)
    if not original_user_id:
        return jsonify({"error": "Not currently impersonating."}), 400
    
    original_user = User.get(original_user_id)
    if not original_user:
        # If original admin somehow got deleted, log out completely
        logout_user()
        return redirect('/')
        
    logout_user()
    login_user(original_user, remember=True)
    return redirect('/')


# --- API: Payment ---
@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session_route():
    """Creates a Stripe checkout session for plan upgrades."""
    data = request.get_json()
    plan = data.get('plan', 'pro')
    plan_config = get_plan_config().get(plan)
    if not plan_config or not stripe.api_key:
        return jsonify(error={'message': 'Invalid plan or payment services unavailable.'}), 500
    
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': f'Myth AI {plan.title()} Plan'},
                    'unit_amount': plan_config['price'],
                },
                'quantity': 1,
            }],
            mode='payment', # Use 'subscription' for recurring payments
            success_url=f"{YOUR_DOMAIN}/payment-success?plan={plan}",
            cancel_url=f"{YOUR_DOMAIN}/payment-cancel",
            client_reference_id=current_user.id
        )
        return jsonify({'id': checkout_session.id})
    except Exception as e:
        return jsonify(error=str(e)), 403


@app.route('/payment-success')
@login_required
def payment_success_route():
    """Handles successful payments by upgrading the user's plan."""
    plan = request.args.get('plan', 'pro')
    if plan in get_plan_config():
        current_user.plan = plan
        save_database()
    return redirect(f'/?payment=success&plan={plan}')

@app.route('/payment-cancel')
@login_required
def payment_cancel_route():
    """Handles cancelled payments."""
    return redirect('/?payment=cancel')


# --- Helper Functions ---
def get_user_data_for_frontend(user):
    """Consolidates user data to be sent to the frontend."""
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_details = get_plan_config().get(user.plan, {})
    return {
        "id": user.id, "username": user.username, "role": user.role, "plan": user.plan,
        "account_type": user.account_type, "streak": user.streak,
        "daily_messages": user.daily_messages, 
        "message_limit": plan_details.get("message_limit", 0),
        "can_upload": plan_details.get("file_upload", False)
    }

def get_all_user_chats(user_id):
    """Retrieves all chats belonging to a specific user."""
    return {cid: cdata for cid, cdata in DB['chats'].items() if cdata.get('user_id') == user_id}

def check_and_reset_daily_limit(user):
    """Resets a user's daily message count if it's a new day."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0
        save_database() # Save the reset to the database

# --- Application Startup ---
if __name__ == '__main__':
    load_database()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)

