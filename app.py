import os
import json
import logging
import base64
import time
import uuid
import secrets
import smtplib
from io import BytesIO
from email.mime.text import MIMEText
from flask import Flask, Response, request, stream_with_context, session, jsonify, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv
import stripe
from PIL import Image
from authlib.integrations.flask_client import OAuth
from itsdangerous import URLSafeTimedSerializer

# --- 1. Initial Configuration ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Security Check for Essential Keys ---
# IMPORTANT: Added new keys for Email and Google OAuth functionality.
REQUIRED_KEYS = [
    'SECRET_KEY', 'GEMINI_API_KEY', 'SECRET_REGISTRATION_KEY',
    'SECRET_STUDENT_KEY', 'SECRET_TEACHER_KEY', 'STRIPE_WEBHOOK_SECRET',
    'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
    'MAIL_SERVER', 'MAIL_PORT', 'MAIL_USERNAME', 'MAIL_PASSWORD', 'MAIL_SENDER'
]
for key in REQUIRED_KEYS:
    if not os.environ.get(key):
        logging.critical(f"CRITICAL ERROR: Environment variable '{key}' is not set. Application cannot start.")
        exit(f"Error: Missing required environment variable '{key}'. Please set it in your .env file.")

# --- Application Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
DATABASE_FILE = 'database.json'
# Required for itsdangerous (password reset tokens)
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get('SECRET_KEY')


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
    "STRIPE_WEBHOOK_SECRET": os.environ.get('STRIPE_WEBHOOK_SECRET'),
    # Google OAuth Config
    "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_CLIENT_ID"),
    "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_CLIENT_SECRET"),
    # Mail Config for Password Reset
    "MAIL_SERVER": os.environ.get('MAIL_SERVER'),
    "MAIL_PORT": int(os.environ.get('MAIL_PORT', 587)),
    "MAIL_USE_TLS": os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', '1', 't'],
    "MAIL_USERNAME": os.environ.get('MAIL_USERNAME'),
    "MAIL_PASSWORD": os.environ.get('MAIL_PASSWORD'),
    "MAIL_SENDER": os.environ.get('MAIL_SENDER'),
}


# --- API & Services Initialization ---
GEMINI_API_CONFIGURED = False
try:
    genai.configure(api_key=SITE_CONFIG["GEMINI_API_KEY"])
    GEMINI_API_CONFIGURED = True
except Exception as e:
    logging.critical(f"Could not configure Gemini API. Details: {e}")

stripe.api_key = SITE_CONFIG["STRIPE_SECRET_KEY"]
if not stripe.api_key:
    logging.warning("Stripe Secret Key is not set. Payment flows will fail.")

oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=SITE_CONFIG['GOOGLE_CLIENT_ID'],
    client_secret=SITE_CONFIG['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

password_reset_serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])


# --- 2. Database Management ---
# NOTE ON PERSISTENCE: This application uses a single JSON file as a database.
# This is convenient for development but is NOT SUITABLE for production environments
# where the filesystem may be ephemeral (e.g., Heroku, some Docker setups).
# For production, you should migrate to a dedicated database like PostgreSQL or MySQL
# using a library like SQLAlchemy.
DB = { "users": {}, "chats": {}, "classrooms": {}, "site_settings": {"announcement": "Welcome! Student and Teacher signups are now available."} }

def save_database():
    """Saves the entire in-memory DB to a JSON file atomically."""
    temp_file = f"{DATABASE_FILE}.tmp"
    try:
        with open(temp_file, 'w') as f:
            serializable_db = {
                "users": {uid: user_to_dict(u) for uid, u in DB['users'].items()},
                "chats": DB['chats'],
                "classrooms": DB['classrooms'],
                "site_settings": DB['site_settings'],
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
            DB['classrooms'] = data.get('classrooms', {})
            # Gracefully handle loading users, ensuring User.from_dict can handle old formats
            DB['users'] = {uid: User.from_dict(u_data) for uid, u_data in data.get('users', {}).items()}
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Could not load database file. Starting fresh. Error: {e}")


# --- 3. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    # For API requests, return JSON. For browser, could redirect.
    if request.path.startswith('/api/'):
        return jsonify({"error": "Login required.", "logged_in": False}), 401
    return redirect(url_for('index'))

class User(UserMixin):
    def __init__(self, id, username, email, password_hash, role='user', plan='free', account_type='general', daily_messages=0, last_message_date=None, classroom_code=None, streak=0, last_streak_date=None):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash # Can be None if using OAuth
        self.role = role
        self.plan = plan
        self.account_type = account_type
        self.daily_messages = daily_messages
        self.last_message_date = last_message_date or datetime.now().strftime("%Y-%m-%d")
        self.classroom_code = classroom_code
        self.streak = streak
        self.last_streak_date = last_streak_date or datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def get(user_id):
        return DB['users'].get(user_id)
        
    @staticmethod
    def get_by_email(email):
        for user in DB['users'].values():
            if user.email and user.email.lower() == email.lower():
                return user
        return None

    @staticmethod
    def get_by_username(username):
        for user in DB['users'].values():
            if user.username.lower() == username.lower():
                return user
        return None

    @staticmethod
    def from_dict(data):
        # Handle loading older user models that may not have the 'email' field
        if 'email' not in data:
            data['email'] = None # or some placeholder
        return User(**data)

def user_to_dict(user):
    return {
        'id': user.id, 'username': user.username, 'email': user.email, 'password_hash': user.password_hash,
        'role': user.role, 'plan': user.plan, 'account_type': user.account_type,
        'daily_messages': user.daily_messages, 'last_message_date': user.last_message_date,
        'classroom_code': user.classroom_code, 'streak': user.streak,
        'last_streak_date': user.last_streak_date
    }

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def initialize_database_defaults():
    made_changes = False
    if not User.get_by_username('admin'):
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'supersecretadminpassword123')
        admin_email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
        admin = User(id='admin', username='admin', email=admin_email, password_hash=generate_password_hash(admin_pass), role='admin', plan='ultra', account_type='general')
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
    "free": {"name": "Free", "price_string": "Free", "features": ["15 Daily Messages", "Standard Model Access", "No Image Uploads"], "color": "text-gray-300", "message_limit": 15, "can_upload": False, "model": "gemini-1.5-flash-latest", "can_tts": False},
    "pro": {"name": "Pro", "price_string": "$9.99 / month", "features": ["50 Daily Messages", "Image Uploads", "Priority Support", "Voice Chat"], "color": "text-indigo-400", "message_limit": 50, "can_upload": True, "model": "gemini-1.5-pro-latest", "can_tts": True},
    "ultra": {"name": "Ultra", "price_string": "$100 one-time", "features": ["Unlimited Messages", "Image Uploads", "Access to All Models", "Voice Chat"], "color": "text-purple-400", "message_limit": 10000, "can_upload": True, "model": "gemini-1.5-pro-latest", "can_tts": True},
    "student": {"name": "Student", "price_string": "$4.99 / month", "features": ["100 Daily Messages", "Image Uploads", "Study Buddy Persona", "Streak & Leaderboard"], "color": "text-amber-400", "message_limit": 100, "can_upload": True, "model": "gemini-1.5-flash-latest", "can_tts": False}
}

rate_limit_store = {}
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
            ip = request.remote_addr
            now = time.time()
            rate_limit_store[ip] = [t for t in rate_limit_store.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
            if len(rate_limit_store.get(ip, [])) >= max_attempts:
                return jsonify({"error": "Too many requests. Please try again later."}), 429
            rate_limit_store.setdefault(ip, []).append(now)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

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
        /* NEW/UPDATED Study Buddy Theme (Yellow/Orange) */
        .study-buddy-mode { background-color: #432500; color: #ffedd5; }
        .study-buddy-mode #sidebar { background: rgba(30, 16, 0, 0.7); }
        .study-buddy-mode #chat-window { color: #fed7aa; }
        .study-buddy-mode .glassmorphism { background: rgba(74, 44, 13, 0.5); border-color: rgba(251, 191, 36, 0.2); }
        .study-buddy-mode .brand-gradient { background-image: linear-gradient(to right, #f59e0b, #f97316); }
        .study-buddy-mode #send-btn { background-image: linear-gradient(to right, #f97316, #ea580c); }
        .study-buddy-mode #user-input { color: #ffedd5; }
        .study-buddy-mode #user-input::placeholder { color: #fbbf24; }
        .study-buddy-mode .message-wrapper .font-bold { color: #fed7aa; }
        .study-buddy-mode .ai-avatar { background-image: linear-gradient(to right, #f59e0b, #f97316); }
        .study-buddy-mode ::-webkit-scrollbar-track { background: #78350f; }
        .study-buddy-mode ::-webkit-scrollbar-thumb { background: #b45309; }
        .study-buddy-mode #sidebar button:hover { background-color: rgba(245, 158, 11, 0.3); }
        .study-buddy-mode #sidebar .bg-blue-600\/30 { background-color: rgba(234, 88, 12, 0.4); }
        .typing-indicator span { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background-color: currentColor; margin: 0 2px; animation: typing-bounce 1.4s infinite ease-in-out both; }
        .typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
        .typing-indicator span:nth-child(2) { animation-delay: -0.16s; }
        @keyframes typing-bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1.0); } }
    </style>
</head>
<body class="font-sans text-gray-200 antialiased">
    <div id="announcement-banner" class="hidden text-center p-2 bg-indigo-600 text-white text-sm"></div>
    <div id="app-container" class="relative h-screen w-screen"></div>
    <div id="modal-container"></div>
    <div id="toast-container" class="fixed top-6 right-6 z-[100] flex flex-col gap-2"></div>

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
                    <div id="email-field-container" class="hidden mb-4">
                        <label for="email" class="block text-sm font-medium text-gray-300 mb-1">Email</label>
                        <input type="email" id="email" name="email" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all">
                    </div>
                    <div class="mb-4">
                        <label for="username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <div class="mb-4">
                        <label for="password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <div class="flex justify-end mb-6">
                        <button type="button" id="forgot-password-link" class="text-xs text-blue-400 hover:text-blue-300">Forgot Password?</button>
                    </div>
                    <button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg transition-opacity">Login</button>
                    <p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>

                <div class="relative flex py-5 items-center">
                    <div class="flex-grow border-t border-gray-600"></div>
                    <span class="flex-shrink mx-4 text-gray-400 text-xs">OR</span>
                    <div class="flex-grow border-t border-gray-600"></div>
                </div>

                <a href="/api/login/google" class="w-full flex items-center justify-center gap-3 bg-white hover:bg-gray-200 text-gray-800 font-bold py-3 px-4 rounded-lg transition-colors">
                    <svg class="w-5 h-5" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"></path><path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"></path><path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"></path><path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571l6.19,5.238C42.021,35.596,44,30.138,44,24C44,22.659,43.862,21.35,43.611,20.083z"></path></svg>
                    Continue with Google
                </a>

                <div class="text-center mt-6">
                    <button id="auth-toggle-btn" class="text-sm text-blue-400 hover:text-blue-300">Don't have an account? Sign Up</button>
                </div>
            </div>
             <div class="text-center mt-4 flex justify-center gap-4">
                 <button id="student-signup-link" class="text-xs text-gray-500 hover:text-gray-400">Student Sign Up</button>
                 <button id="teacher-signup-link" class="text-xs text-gray-500 hover:text-gray-400">Teacher Sign Up</button>
                 <button id="special-auth-link" class="text-xs text-gray-500 hover:text-gray-400">Admin Portal</button>
             </div>
        </div>
    </template>
    
    <template id="template-reset-password-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="reset-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2">Reset Your Password</h2>
                <p class="text-gray-400 text-center mb-8">Enter a new password for your account.</p>
                <form id="reset-password-form">
                    <div class="mb-4">
                        <label for="new-password" class="block text-sm font-medium text-gray-300 mb-1">New Password</label>
                        <input type="password" id="new-password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg">Set New Password</button>
                    <p id="reset-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
            </div>
        </div>
    </template>
    
    <template id="template-student-signup-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="student-signup-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2">Student Account Signup</h2>
                <p class="text-gray-400 text-center mb-8">Create a student account to join a classroom.</p>
                <form id="student-signup-form">
                    <div class="mb-4">
                        <label for="student-email" class="block text-sm font-medium text-gray-300 mb-1">Email</label>
                        <input type="email" id="student-email" name="email" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="student-username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="student-username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="student-password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="student-password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-6">
                        <label for="student-classroom-code" class="block text-sm font-medium text-gray-300 mb-1">Classroom Code</label>
                        <input type="text" id="student-classroom-code" name="classroom_code" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-green-500 to-teal-500 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg">Create Student Account</button>
                    <p id="student-signup-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
            </div>
            <div class="text-center mt-4">
                <button id="back-to-main-login" class="text-xs text-gray-500 hover:text-gray-400">Back to Main Login</button>
            </div>
        </div>
    </template>
    
    <template id="template-teacher-signup-page">
        <div class="flex flex-col items-center justify-center h-full w-full bg-gray-900 p-4">
            <div class="w-full max-w-md glassmorphism rounded-2xl p-8 shadow-2xl animate-scale-up">
                <div class="flex justify-center mb-6" id="teacher-signup-logo-container"></div>
                <h2 class="text-3xl font-bold text-center text-white mb-2">Teacher Account Signup</h2>
                <p class="text-gray-400 text-center mb-8">Create a teacher account to manage student progress.</p>
                <form id="teacher-signup-form">
                    <div class="mb-4">
                        <label for="teacher-email" class="block text-sm font-medium text-gray-300 mb-1">Email</label>
                        <input type="email" id="teacher-email" name="email" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="teacher-username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="teacher-username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="teacher-password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="teacher-password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-6">
                        <label for="teacher-secret-key" class="block text-sm font-medium text-gray-300 mb-1">Teacher Access Key</label>
                        <input type="password" id="teacher-secret-key" name="secret_key" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <button type="submit" class="w-full bg-gradient-to-r from-blue-500 to-indigo-500 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg">Create Teacher Account</button>
                    <p id="teacher-signup-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
            </div>
            <div class="text-center mt-4">
                <button id="back-to-main-login" class="text-xs text-gray-500 hover:text-gray-400">Back to Main Login</button>
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
                <div id="study-mode-toggle-container" class="hidden flex-shrink-0 p-2 mb-2"></div>
                
                <div class="flex-shrink-0"><button id="new-chat-btn" class="w-full text-left flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/50 transition-colors duration-200"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14" /><path d="M5 12h14" /></svg> New Chat</button></div>
                <div id="chat-history-list" class="flex-grow overflow-y-auto my-4 space-y-1 pr-1"></div>
                
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
                        <button id="download-chat-btn" title="Download Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg></button>
                    </div>
                </header>
                <div id="chat-window" class="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 min-h-0"></div>
                <div class="flex-shrink-0 p-2 md:p-4 md:px-6 border-t border-gray-700/50">
                    <div class="max-w-4xl mx-auto">
                         <div id="student-leaderboard-container" class="glassmorphism p-4 rounded-lg hidden mb-2"></div>
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
           <div id="plans-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-8 max-w-7xl mx-auto">
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

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
            <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Total Users</h2><p id="admin-total-users" class="text-4xl font-bold text-white">0</p></div>
            <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Pro Users</h2><p id="admin-pro-users" class="text-4xl font-bold text-white">0</p></div>
            <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Ultra Users</h2><p id="admin-ultra-users" class="text-4xl font-bold text-white">0</p></div>
        </div>

        <div class="p-6 glassmorphism rounded-lg">
            <h2 class="text-xl font-semibold mb-4 text-white">User Management</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left">
                    <thead class="border-b border-gray-600">
                        <tr>
                            <th class="p-2">Username</th>
                            <th class="p-2">Email</th>
                            <th class="p-2">Role</th>
                            <th class="p-2">Plan</th>
                            <th class="p-2">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="admin-user-list"></tbody>
                </table>
            </div>
        </div>
    </div>
    </template>
   
    <template id="template-teacher-dashboard">
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
    
    <script>
/****************************************************************************
 * JAVASCRIPT FRONTEND LOGIC (Myth AI - Revamped)
 ****************************************************************************/
document.addEventListener('DOMContentLoaded', () => {
    const appState = {
        chats: {}, activeChatId: null, isAITyping: false,
        abortController: null, currentUser: null,
        isStudyMode: false, uploadedFile: null,
        teacherData: { classroom: null, students: [] },
        audio: null,
    };

    const DOMElements = {
        appContainer: document.getElementById('app-container'),
        modalContainer: document.getElementById('modal-container'),
        toastContainer: document.getElementById('toast-container'),
        announcementBanner: document.getElementById('announcement-banner'),
    };
    
    // --- ROUTER & INITIALIZER ---
    const routeHandler = async () => {
        const path = window.location.pathname;
        const urlParams = new URLSearchParams(window.location.search);

        if (path.startsWith('/reset-password/')) {
            const token = path.split('/')[2];
            renderResetPasswordPage(token);
        } else {
            // Handle post-payment redirects
            if (urlParams.get('payment') === 'success') {
                showToast('Upgrade successful!', 'success');
                window.history.replaceState({}, document.title, "/");
            } else if (urlParams.get('payment') === 'cancel') {
                showToast('Payment was cancelled.', 'info');
                window.history.replaceState({}, document.title, "/");
            }
            // Default action: check login status
            await checkLoginStatus();
        }
    };
    
    // --- UTILITY FUNCTIONS ---
    function showToast(message, type = 'info') {
        const colors = { info: 'bg-blue-600', success: 'bg-green-600', error: 'bg-red-600' };
        const toast = document.createElement('div');
        toast.className = `toast text-white text-sm py-2 px-4 rounded-lg shadow-lg animate-fade-in ${colors[type]}`;
        toast.textContent = message;
        DOMElements.toastContainer.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.addEventListener('transitionend', () => toast.remove());
        }, 4000);
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
            // Ensure headers are set up correctly
            const headers = { ...(options.headers || {}) };
            if (!headers['Content-Type'] && options.body && typeof options.body === 'string') {
                headers['Content-Type'] = 'application/json';
            }

            const response = await fetch(endpoint, {
                ...options,
                headers,
                credentials: 'include' // Important for sessions/cookies
            });

            const data = response.headers.get("Content-Type")?.includes("application/json") ? await response.json() : null;

            if (!response.ok) {
                if (response.status === 401 && data?.error === "Login required.") {
                    handleLogout(false); // Force logout if session is invalid
                }
                throw new Error(data?.error || `Server error: ${response.statusText}`);
            }
            return { success: true, ...(data || {}) };
        } catch (error) {
            console.error("API Call Error:", endpoint, error);
            showToast(error.message, 'error');
            return { success: false, error: error.message };
        }
    }

    function openModal(title, bodyContent, onConfirm, confirmText = 'Confirm') {
        closeModal(); // Close any existing modal first
        const template = document.getElementById('template-modal');
        const modalWrapper = document.createElement('div');
        modalWrapper.id = 'modal-instance';
        modalWrapper.appendChild(template.content.cloneNode(true));
        DOMElements.modalContainer.appendChild(modalWrapper);
        
        modalWrapper.querySelector('#modal-title').textContent = title;
        const modalBody = modalWrapper.querySelector('#modal-body');
        modalBody.innerHTML = ''; // Clear previous content

        if (typeof bodyContent === 'string') {
            modalBody.innerHTML = `<p>${bodyContent}</p>`;
        } else {
            modalBody.appendChild(bodyContent);
        }

        if (onConfirm) {
            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'w-full mt-6 bg-blue-600 hover:bg-blue-500 text-white font-bold py-2 px-4 rounded-lg';
            confirmBtn.textContent = confirmText;
            confirmBtn.onclick = () => { onConfirm(); closeModal(); };
            modalBody.appendChild(confirmBtn);
        }

        const doClose = () => modalWrapper.remove();
        modalWrapper.querySelector('.close-modal-btn').addEventListener('click', doClose);
        modalWrapper.querySelector('.modal-backdrop').addEventListener('click', doClose);
    }
    
    function closeModal() {
        document.getElementById('modal-instance')?.remove();
    }
    
    // --- NEW PASSWORD RESET & AUTH ---

    function renderResetPasswordPage(token) {
        const template = document.getElementById('template-reset-password-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('reset-logo-container');

        const form = document.getElementById('reset-password-form');
        form.onsubmit = async (e) => {
            e.preventDefault();
            const password = document.getElementById('new-password').value;
            const errorEl = document.getElementById('reset-error');
            errorEl.textContent = '';
            
            if (password.length < 6) {
                errorEl.textContent = 'Password must be at least 6 characters.';
                return;
            }

            const result = await apiCall('/api/reset-with-token', {
                method: 'POST',
                body: JSON.stringify({ token, password }),
            });

            if (result.success) {
                showToast('Password updated successfully! Please log in.', 'success');
                setTimeout(() => window.location.href = '/', 2000);
            } else {
                errorEl.textContent = result.error;
            }
        };
    }

    function handleForgotPassword() {
        const body = document.createElement('div');
        body.innerHTML = `
            <p class="mb-4 text-gray-400">Enter your email address and we'll send you a link to reset your password.</p>
            <input type="email" id="forgot-email-input" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" placeholder="your@email.com">
            <p id="forgot-error" class="text-red-400 text-sm h-4 mt-2"></p>
        `;
        openModal('Forgot Password', body, async () => {
            const email = document.getElementById('forgot-email-input').value;
            const errorEl = document.getElementById('forgot-error');
            errorEl.textContent = '';
            if (!email) {
                errorEl.textContent = 'Email is required.';
                return;
            }
            const result = await apiCall('/api/request-password-reset', {
                method: 'POST',
                body: JSON.stringify({ email }),
            });
            if (result.success) {
                closeModal();
                showToast(result.message, 'success');
            } else {
                errorEl.textContent = result.error;
            }
        }, 'Send Reset Link');
    }

    function renderAuthPage(isLogin = true) {
        const template = document.getElementById('template-auth-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('auth-logo-container');
        
        const emailField = document.getElementById('email-field-container');
        emailField.classList.toggle('hidden', isLogin);

        document.getElementById('auth-title').textContent = isLogin ? 'Welcome Back' : 'Create Account';
        document.getElementById('auth-subtitle').textContent = isLogin ? 'Sign in to continue to Myth AI.' : 'Create a new general account.';
        document.getElementById('auth-submit-btn').textContent = isLogin ? 'Login' : 'Sign Up';
        document.getElementById('auth-toggle-btn').textContent = isLogin ? "Don't have an account? Sign Up" : "Already have an account? Login";
        document.getElementById('forgot-password-link').style.display = isLogin ? 'block' : 'none';

        document.getElementById('auth-toggle-btn').onclick = () => renderAuthPage(!isLogin);
        document.getElementById('forgot-password-link').onclick = handleForgotPassword;
        document.getElementById('student-signup-link').onclick = renderStudentSignupPage;
        document.getElementById('teacher-signup-link').onclick = renderTeacherSignupPage;
        document.getElementById('special-auth-link').onclick = renderSpecialAuthPage;

        document.getElementById('auth-form').onsubmit = async (e) => {
            e.preventDefault();
            const form = e.target;
            const errorEl = document.getElementById('auth-error');
            errorEl.textContent = '';
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            
            // On signup, require email.
            if (!isLogin && (!data.email || !data.email.includes('@'))) {
                 errorEl.textContent = 'A valid email is required to sign up.';
                 return;
            }
            
            const endpoint = isLogin ? '/api/login' : '/api/signup';
            
            const result = await apiCall(endpoint, {
                method: 'POST',
                body: JSON.stringify(data),
            });

            if (result.success) {
                initializeApp(result.user, result.chats, result.settings);
            } else {
                errorEl.textContent = result.error;
            }
        };
    }
    
    // ... Other render functions like renderStudentSignupPage, etc. are largely the same but with added email field.
    // I will show the change for renderStudentSignupPage as an example
    function renderStudentSignupPage() {
        const template = document.getElementById('template-student-signup-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('student-signup-logo-container');
        document.getElementById('back-to-main-login').onclick = () => renderAuthPage(true);
        
        document.getElementById('student-signup-form').onsubmit = async (e) => {
            e.preventDefault();
            const form = e.target;
            const errorEl = document.getElementById('student-signup-error');
            errorEl.textContent = '';
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            
            const result = await apiCall('/api/student_signup', {
                method: 'POST',
                body: JSON.stringify(data),
            });

            if (result.success) {
                initializeApp(result.user, result.chats, result.settings);
            } else {
                errorEl.textContent = result.error;
            }
        };
    }

    async function checkLoginStatus() {
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

        // Routing to the correct dashboard
        if (user.role === 'admin') {
            renderAdminDashboard();
        } else if (user.account_type === 'teacher') {
            renderTeacherDashboard();
        } else {
            renderAppUI();
        }
    }

    // --- MAIN CHAT LOGIC ---

    // REFACTORED for better error handling and state management
    async function handleSendMessage() {
        const userInput = document.getElementById('user-input');
        if (!userInput) return;
        const prompt = userInput.value.trim();
        if ((!prompt && !appState.uploadedFile) || appState.isAITyping) return;

        // Set state immediately
        appState.isAITyping = true;
        appState.abortController = new AbortController();
        updateUIState();
        
        // This single try/finally ensures state is ALWAYS reset
        try {
            // 1. Ensure a chat exists
            if (!appState.activeChatId) {
                const chatCreated = await createNewChat(false);
                if (!chatCreated) {
                    throw new Error("Could not start a new chat session.");
                }
            }
            
            // 2. Clear welcome screen if it's the first message
            if (appState.chats[appState.activeChatId]?.messages.length === 0) {
                document.getElementById('chat-window').innerHTML = '';
            }

            // 3. Add user message to DOM
            addMessageToDOM({ sender: 'user', content: prompt });
            
            // 4. Create placeholder for AI response
            const aiMessage = { sender: 'model', content: '' };
            const aiContentEl = addMessageToDOM(aiMessage, true).querySelector('.message-content');

            // 5. Prepare form data (prompt + file)
            const formData = new FormData();
            formData.append('chat_id', appState.activeChatId);
            formData.append('prompt', prompt);
            formData.append('is_study_mode', appState.isStudyMode);
            if (appState.uploadedFile) {
                formData.append('file', appState.uploadedFile);
            }
            
            // 6. Clear input and reset state for next message
            userInput.value = '';
            userInput.style.height = 'auto';
            appState.uploadedFile = null;
            updatePreviewContainer();
            
            // 7. Make the streaming API call
            const response = await fetch('/api/chat', {
                method: 'POST',
                body: formData,
                signal: appState.abortController.signal,
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || `Server error: ${response.status}`);
            }

            // 8. Process the stream
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let fullResponse = '';
            const chatWindow = document.getElementById('chat-window');

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                const chunk = decoder.decode(value, { stream: true });
                fullResponse += chunk;
                aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse + '<span class="animate-pulse">▍</span>'));
                if(chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
            }
            
            // 9. Finalize AI message in DOM
            aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse || "Sorry, an empty response was received."));
            renderCodeCopyButtons();
            
            // 10. Sync state with server to get updated message counts, new chat title, etc.
            const updatedData = await apiCall('/api/status');
            if (updatedData.success) {
                appState.currentUser = updatedData.user;
                appState.chats = updatedData.chats;
                renderChatHistoryList();
                updateUserInfo();
                document.getElementById('chat-title').textContent = appState.chats[appState.activeChatId].title;
            }

        } catch (error) {
            if (error.name !== 'AbortError') {
                console.error("Error during message sending:", error);
                showToast(error.message, 'error');
                // Optionally display error in the chat window
                const chatWindow = document.getElementById('chat-window');
                const lastMessage = chatWindow.lastElementChild?.querySelector('.message-content');
                if (lastMessage && lastMessage.querySelector('.typing-indicator')) {
                   lastMessage.innerHTML = `<p class="text-red-400"><strong>Error:</strong> ${error.message}</p>`;
                }
            }
        } finally {
            // ALWAYS reset the UI state
            appState.isAITyping = false;
            appState.abortController = null;
            updateUIState();
        }
    }

    // --- OTHER UI & EVENT FUNCTIONS ---
    // Most other JS functions like addMessageToDOM, setupAppEventListeners, etc.
    // remain largely the same, but with slight modifications to call the new
    // auth functions where appropriate. The existing logic was mostly sound.

    async function handleLogout(doApiCall = true) {
        if(doApiCall) await apiCall('/api/logout');
        appState.currentUser = null;
        appState.chats = {};
        appState.activeChatId = null;
        DOMElements.announcementBanner.classList.add('hidden');
        renderAuthPage();
    }
    
    // --- ENTRY POINT ---
    routeHandler(); // Start the app by handling the initial URL
});

// Stubs for other JS functions for brevity
function renderAppUI() { /* as before */ }
function renderActiveChat() { /* as before */ }
function renderWelcomeScreen() { /* as before */ }
function renderChatHistoryList() { /* as before */ }
function updateUserInfo() { /* as before */ }
function updateUIState() { /* as before */ }
function renderStudyModeToggle() { /* as before */ }
function updatePreviewContainer() { /* as before */ }
function addMessageToDOM(msg, isStreaming = false) { /* as before */ }
async function createNewChat(shouldRender = true) { /* as before */ }
function renderCodeCopyButtons() { /* as before */ }
function setupAppEventListeners() { /* as before, but ensure it calls new functions like handleForgotPassword */ }
function renderAdminDashboard() { /* as before */ }
// etc...

</script>
</body>
</html>
"""

# --- 7. Backend Helper Functions ---

def send_password_reset_email(user):
    """Generates a reset token and sends the email."""
    try:
        token = password_reset_serializer.dumps(user.email, salt='password-reset-salt')
        reset_url = url_for('index', _external=True) + f"reset-password/{token}"
        
        msg_body = f"Hello {user.username},\n\nPlease click the following link to reset your password:\n{reset_url}\n\nIf you did not request this, please ignore this email."
        msg = MIMEText(msg_body)
        msg['Subject'] = 'Password Reset Request for Myth AI'
        msg['From'] = SITE_CONFIG['MAIL_SENDER']
        msg['To'] = user.email

        with smtplib.SMTP(SITE_CONFIG['MAIL_SERVER'], SITE_CONFIG['MAIL_PORT']) as server:
            if SITE_CONFIG['MAIL_USE_TLS']:
                server.starttls()
            server.login(SITE_G['MAIL_USERNAME'], SITE_CONFIG['MAIL_PASSWORD'])
            server.send_message(msg)
        logging.info(f"Password reset email sent to {user.email}")
        return True
    except Exception as e:
        logging.error(f"Failed to send password reset email to {user.email}: {e}")
        return False

def check_and_reset_daily_limit(user):
    """Resets a user's daily message count if the day has changed."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0
        
        if user.account_type == 'student':
            try:
                last_streak_date = datetime.strptime(user.last_streak_date, "%Y-%m-%d")
                if (datetime.now() - last_streak_date).days > 1:
                    user.streak = 0
            except (ValueError, TypeError):
                 user.streak = 0 # Reset if date is invalid
            
        save_database()

def get_user_data_for_frontend(user):
    """Prepares user data for sending to the frontend."""
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_details = PLAN_CONFIG.get(user.plan, PLAN_CONFIG['free'])
    
    data = {
        "id": user.id, "username": user.username, "email": user.email, "role": user.role, "plan": user.plan,
        "account_type": user.account_type, "daily_messages": user.daily_messages,
        "message_limit": plan_details["message_limit"], "can_upload": plan_details["can_upload"],
        "is_student_in_class": user.account_type == 'student' and user.classroom_code is not None,
        "streak": user.streak,
    }
    return data

def get_all_user_chats(user_id):
    """Retrieves all chats belonging to a specific user."""
    return {chat_id: chat_data for chat_id, chat_data in DB['chats'].items() if chat_data.get('user_id') == user_id}

def generate_unique_classroom_code():
    while True:
        code = secrets.token_hex(4).upper()
        if code not in DB['classrooms']:
            return code


# --- 8. Core API Routes (Auth, Status, etc.) ---
@app.route('/')
@app.route('/reset-password/<token>')
def index(token=None):
    return Response(HTML_CONTENT, mimetype='text/html')

# ... other routes like /api/config ...

@app.route('/api/signup', methods=['POST'])
@rate_limited()
def signup():
    data = request.get_json()
    if not data: return jsonify({"error": "Invalid request format."}), 400
    
    username = data.get('username', '').strip()
    password = data.get('password', '')
    email = data.get('email', '').strip().lower()

    if not all([username, password, email]) or len(username) < 3 or len(password) < 6 or '@' not in email:
        return jsonify({"error": "Valid email, username (min 3 chars), and password (min 6 chars) are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    if User.get_by_email(email):
        return jsonify({"error": "Email already in use."}), 409
        
    try:
        new_user = User(id=username, username=username, email=email, password_hash=generate_password_hash(password), account_type='general')
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(new_user),
            "chats": {}, "settings": DB['site_settings']
        })
    except Exception as e:
        logging.error(f"Error during signup for {username}: {e}")
        return jsonify({"error": "An internal server error occurred."}), 500

# ... student_signup and teacher_signup should be similarly updated to require email ...

@app.route('/api/login', methods=['POST'])
@rate_limited()
def login():
    data = request.get_json()
    if not data: return jsonify({"error": "Invalid request format."}), 400

    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    
    # Check for password hash existence for users created via OAuth
    if user and user.password_hash and check_password_hash(user.password_hash, password):
        login_user(user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id) if user.role not in ['admin', 'teacher'] else {},
            "settings": DB['site_settings']
        })
    return jsonify({"error": "Invalid username or password."}), 401
    
# --- NEW Google OAuth Routes ---
@app.route('/api/login/google')
def google_login():
    redirect_uri = url_for('authorize', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    try:
        token = oauth.google.authorize_access_token()
        user_info = oauth.google.get('openid/userinfo').json()
    except Exception as e:
        logging.error(f"Google OAuth failed: {e}")
        return redirect(url_for('index')) # Redirect home on error

    email = user_info.get('email', '').lower()
    username = user_info.get('name', email.split('@')[0])

    if not email:
        # Handle case where email is not provided
        return redirect(url_for('index'))

    # 1. Find user by email
    user = User.get_by_email(email)

    if user:
        # User exists, log them in
        login_user(user, remember=True)
    else:
        # 2. User does not exist, create a new one
        # Ensure username is unique
        base_username = username
        while User.get_by_username(username):
            username = f"{base_username}{secrets.token_hex(2)}"
            
        new_user = User(
            id=username,
            username=username,
            email=email,
            password_hash=None, # No password, login is via Google
            account_type='general',
            plan='free'
        )
        DB['users'][new_user.id] = new_user
        save_database()
        login_user(new_user, remember=True)

    return redirect(url_for('index'))

# --- NEW Password Reset Routes ---
@app.route('/api/request-password-reset', methods=['POST'])
@rate_limited()
def request_password_reset():
    data = request.get_json()
    email = data.get('email', '').lower()
    user = User.get_by_email(email)
    if user:
        if send_password_reset_email(user):
            return jsonify({"success": True, "message": "If an account with that email exists, a reset link has been sent."})
        else:
            return jsonify({"error": "Could not send email. Please contact support."}), 500
    # Always return success to prevent user enumeration
    return jsonify({"success": True, "message": "If an account with that email exists, a reset link has been sent."})

@app.route('/api/reset-with-token', methods=['POST'])
@rate_limited()
def reset_with_token():
    data = request.get_json()
    token = data.get('token')
    password = data.get('password')
    if not all([token, password]):
        return jsonify({"error": "Token and new password are required."}), 400

    try:
        email = password_reset_serializer.loads(token, salt='password-reset-salt', max_age=3600) # 1 hour expiry
    except Exception:
        return jsonify({"error": "The password reset link is invalid or has expired."}), 400

    user = User.get_by_email(email)
    if not user:
        return jsonify({"error": "User not found."}), 404
        
    user.password_hash = generate_password_hash(password)
    save_database()
    return jsonify({"success": True, "message": "Password has been updated."})

# ... other existing routes like /api/logout, /api/status ...

# --- Stripe Webhook (IMPROVED) ---
@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = SITE_CONFIG['STRIPE_WEBHOOK_SECRET']

    if not all([payload, sig_header, endpoint_secret]):
        return 'Missing data for webhook', 400

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        logging.warning("Stripe webhook error: Invalid payload")
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        logging.warning("Stripe webhook error: Invalid signature")
        return 'Invalid signature', 400

    event_type = event['type']
    data_object = event['data']['object']
    
    logging.info(f"Received Stripe webhook event: {event_type}")

    if event_type == 'checkout.session.completed':
        client_reference_id = data_object.get('client_reference_id')
        user = User.get(client_reference_id)

        if user:
            line_item = stripe.checkout.Session.list_line_items(data_object.id, limit=1).data[0]
            price_id = line_item.price.id
            
            new_plan = None
            if price_id == SITE_CONFIG["STRIPE_PRO_PRICE_ID"]: new_plan = 'pro'
            elif price_id == SITE_CONFIG["STRIPE_ULTRA_PRICE_ID"]: new_plan = 'ultra'
            elif price_id == SITE_CONFIG["STRIPE_STUDENT_PRICE_ID"]: new_plan = 'student'

            if new_plan:
                user.plan = new_plan
                save_database()
                logging.info(f"User {user.id} successfully upgraded to {new_plan} plan via webhook.")
        else:
            logging.error(f"Webhook checkout.session.completed: User not found for client_reference_id {client_reference_id}")

    elif event_type == 'customer.subscription.deleted':
        # This event fires when a subscription is canceled and the period ends.
        subscription = data_object
        # We need to find the user associated with this subscription.
        # This is harder without storing stripe_customer_id on our User model.
        # A robust solution would store this ID. A simpler (but less reliable) way is to find user by email.
        customer = stripe.Customer.retrieve(subscription.customer)
        user = User.get_by_email(customer.email)
        if user:
            # Downgrade the user to the free plan
            if user.plan != 'ultra': # Don't downgrade one-time ultra purchasers
                user.plan = 'free'
                save_database()
                logging.info(f"Subscription for user {user.id} ended. Downgraded to free plan.")
        else:
            logging.warning(f"Webhook customer.subscription.deleted: Could not find user with email {customer.email}")

    return 'Success', 200


# --- Main Execution ---
if __name__ == '__main__':
    # Use debug=True only for development. Flask's reloader is helpful.
    # For production, use a proper WSGI server like Gunicorn or uWSGI.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
