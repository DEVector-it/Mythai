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
# Core keys required for the application to run at all.
REQUIRED_KEYS = [
    'SECRET_KEY', 'GEMINI_API_KEY', 'SECRET_REGISTRATION_KEY',
    'SECRET_STUDENT_KEY', 'SECRET_TEACHER_KEY', 'STRIPE_WEBHOOK_SECRET',
]
for key in REQUIRED_KEYS:
    if not os.environ.get(key):
        logging.critical(f"CRITICAL ERROR: Environment variable '{key}' is not set. Application cannot start.")
        exit(f"Error: Missing required environment variable '{key}'. Please set it in your .env file.")

# --- Application Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
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

# --- Feature Flags based on Environment Variables ---
GOOGLE_OAUTH_ENABLED = all([SITE_CONFIG['GOOGLE_CLIENT_ID'], SITE_CONFIG['GOOGLE_CLIENT_SECRET']])
EMAIL_ENABLED = all([SITE_CONFIG['MAIL_SERVER'], SITE_CONFIG['MAIL_USERNAME'], SITE_CONFIG['MAIL_PASSWORD']])

oauth = OAuth(app)
if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name='google',
        client_id=SITE_CONFIG['GOOGLE_CLIENT_ID'],
        client_secret=SITE_CONFIG['GOOGLE_CLIENT_SECRET'],
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
    logging.info("Google OAuth has been configured and enabled.")
else:
    logging.warning("Google OAuth credentials not found in .env file. Google Sign-In will be disabled.")

password_reset_serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
if not EMAIL_ENABLED:
    logging.warning("Email server credentials not found in .env file. Password reset functionality will be disabled.")
else:
    logging.info("Email server has been configured and enabled.")


# --- 2. Database Management (ENHANCED FOR SAFETY) ---
# The DATA_DIR should NOT be in version control (e.g., add 'data/' to your .gitignore file).
DATA_DIR = 'data'
DATABASE_FILE = os.path.join(DATA_DIR, 'database.json')
DB = { "users": {}, "chats": {}, "classrooms": {}, "site_settings": {"announcement": "Welcome! Student and Teacher signups are now available."} }

def setup_database_dir():
    """Ensures the data directory and .gitignore exist to protect user data."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        logging.info(f"Created data directory at: {DATA_DIR}")
        # Create a .gitignore file in the data directory to prevent accidental commits of user data.
        gitignore_path = os.path.join(DATA_DIR, '.gitignore')
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, 'w') as f:
                f.write('*\n')
                f.write('!.gitignore\n')
            logging.info(f"Created .gitignore in {DATA_DIR} to protect database files.")

def save_database():
    """Saves the DB to a JSON file atomically and creates a backup for safety."""
    setup_database_dir()

    # Backup existing database before saving. This prevents data loss on write errors.
    if os.path.exists(DATABASE_FILE):
        backup_file = os.path.join(DATA_DIR, f"database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json.bak")
        try:
            # os.rename is atomic on most systems
            os.rename(DATABASE_FILE, backup_file)
            # Clean up old backups, keeping the 5 most recent ones.
            backups = sorted([f for f in os.listdir(DATA_DIR) if f.endswith('.bak')], reverse=True)
            for old_backup in backups[5:]:
                os.remove(os.path.join(DATA_DIR, old_backup))
        except Exception as e:
            logging.error(f"Could not create database backup: {e}")

    # Save current database atomically using a temporary file.
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
        os.replace(temp_file, DATABASE_FILE) # Atomic operation
    except Exception as e:
        logging.error(f"FATAL: Failed to save database: {e}")
        # If save fails, try to restore the most recent backup immediately.
        if 'backup_file' in locals() and os.path.exists(backup_file):
            os.rename(backup_file, DATABASE_FILE)
            logging.info("Restored database from immediate backup after save failure.")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def load_database():
    """Loads the database from JSON, with fallback to the most recent backup."""
    global DB
    setup_database_dir()
    if not os.path.exists(DATABASE_FILE):
        logging.warning(f"Database file not found at {DATABASE_FILE}. A new one will be created on first save.")
        return

    try:
        with open(DATABASE_FILE, 'r') as f:
            data = json.load(f)
        DB['chats'] = data.get('chats', {})
        DB['site_settings'] = data.get('site_settings', {"announcement": ""})
        DB['classrooms'] = data.get('classrooms', {})
        DB['users'] = {uid: User.from_dict(u_data) for uid, u_data in data.get('users', {}).items()}
        logging.info(f"Successfully loaded database from {DATABASE_FILE}")
    except (json.JSONDecodeError, FileNotFoundError, TypeError) as e:
        logging.error(f"Could not load main database file '{DATABASE_FILE}'. Error: {e}")
        # Attempt to load the most recent backup if main file is corrupt/missing.
        backups = sorted([f for f in os.listdir(DATA_DIR) if f.endswith('.bak')], reverse=True)
        if backups:
            backup_to_load = os.path.join(DATA_DIR, backups[0])
            logging.info(f"Attempting to load most recent backup: {backup_to_load}")
            try:
                with open(backup_to_load, 'r') as f:
                    data = json.load(f)
                DB['chats'] = data.get('chats', {})
                DB['site_settings'] = data.get('site_settings', {"announcement": ""})
                DB['classrooms'] = data.get('classrooms', {})
                DB['users'] = {uid: User.from_dict(u_data) for uid, u_data in data.get('users', {}).items()}
                # If backup is loaded successfully, restore it as the main DB file
                os.rename(backup_to_load, DATABASE_FILE)
                logging.info(f"SUCCESS: Loaded and restored from backup file {backups[0]}")
            except Exception as backup_e:
                logging.error(f"FATAL: Failed to load backup file as well. Starting with a fresh database. Error: {backup_e}")
        else:
            logging.warning("No backups found. Starting with a fresh database.")


# --- 3. User and Session Management ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({"error": "Login required.", "logged_in": False}), 401
    return redirect(url_for('index'))

class User(UserMixin):
    """User model now includes an email field for password reset and Google OAuth."""
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
        if not email: return None
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
        # Handles loading older user models that may not have all fields.
        data.setdefault('email', None)
        data.setdefault('password_hash', None)
        data.setdefault('role', 'user')
        data.setdefault('plan', 'free')
        data.setdefault('account_type', 'general')
        data.setdefault('daily_messages', 0)
        data.setdefault('last_message_date', datetime.now().strftime("%Y-%m-%d"))
        data.setdefault('classroom_code', None)
        data.setdefault('streak', 0)
        data.setdefault('last_streak_date', datetime.now().strftime("%Y-%m-%d"))
        return User(**data)

def user_to_dict(user):
    """Serializes the User object to a dictionary."""
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

# Initial load of the application
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
RATE_LIMIT_WINDOW = 60

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
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1136294351029434"
     crossorigin="anonymous"></script>
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
        /* Study Buddy Theme */
        .study-buddy-mode { background-color: #432500; color: #ffedd5; }
        .study-buddy-mode #sidebar { background: rgba(30, 16, 0, 0.7); color: #fed7aa; }
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
        .study-buddy-mode #sidebar .bg-blue-600\\/30 { background-color: rgba(234, 88, 12, 0.4); }
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
                        <button type="button" id="forgot-password-link" class="text-xs text-blue-400 hover:text-blue-300 hidden">Forgot Password?</button>
                    </div>
                    <button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg transition-opacity">Login</button>
                    <p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>

                <div id="google-auth-container" class="hidden">
                    <div class="relative flex py-5 items-center">
                        <div class="flex-grow border-t border-gray-600"></div>
                        <span class="flex-shrink mx-4 text-gray-400 text-xs">OR</span>
                        <div class="flex-grow border-t border-gray-600"></div>
                    </div>

                    <a href="/api/login/google" class="w-full flex items-center justify-center gap-3 bg-white hover:bg-gray-200 text-gray-800 font-bold py-3 px-4 rounded-lg transition-colors">
                        <svg class="w-5 h-5" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z"></path><path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z"></path><path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z"></path><path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571l6.19,5.238C42.021,35.596,44,30.138,44,24C44,22.659,43.862,21.35,43.611,20.083z"></path></svg>
                        Continue with Google
                    </a>
                </div>

                <div class="text-center mt-6">
                    <button id="auth-toggle-btn" class="text-sm text-blue-400 hover:text-blue-300">Don't have an account? Sign Up</button>
                </div>
            </div>
             <div class="text-center mt-4 flex justify-center gap-4">
                 <button id="student-signup-link" class="text-xs text-gray-500 hover:text-gray-400">Student Sign Up</button>
                 <button id="teacher-signup-link" class="text-xs text-gray-500 hover:text-gray-400">Teacher Sign Up</button>
                 <button id="special-auth-link" class="text-xs text-gray-500 hover:text-gray-400">Admin Portal</button>
             </div>
             <p class="text-xs text-gray-600 mt-8">Made by DeVector</p>
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
            <main class="flex-1 flex flex-col bg-gray-800 h-full min-w-0">
                <header class="flex-shrink-0 p-4 flex items-center justify-between border-b border-gray-700/50">
                    <div class="flex items-center gap-2 min-w-0">
                        <button id="menu-toggle-btn" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors md:hidden">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
                        </button>
                        <h2 id="chat-title" class="text-xl font-semibold truncate">New Chat</h2>
                    </div>
                    <div class="flex items-center gap-1 sm:gap-2">
                        <button id="share-chat-btn" title="Share Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button>
                        <button id="rename-chat-btn" title="Rename Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg></button>
                        <button id="delete-chat-btn" title="Delete Chat" class="p-2 rounded-lg hover:bg-red-500/20 text-red-400 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" /></svg></button>
                        <button id="download-chat-btn" title="Download Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg></button>
                    </div>
                </header>
                <!-- **UI CHANGE**: The chat window now has an inner container for messages to control width on large screens -->
                <div id="chat-window" class="flex-1 overflow-y-auto p-4 md:p-6 min-h-0 w-full">
                    <div id="message-list" class="mx-auto max-w-4xl space-y-6">
                        <!-- Messages will be rendered here by JavaScript -->
                    </div>
                </div>
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
    
    <template id="template-modal">
        <div class="modal-backdrop fixed inset-0 bg-black/60 animate-fade-in z-50"></div>
        <div class="modal-content fixed inset-0 flex items-center justify-center p-4 z-50">
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
                <p class="text-gray-400 text-center mb-8">Create an Admin account.</p>
                <form id="special-auth-form">
                    <div class="mb-4">
                        <label for="special-username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="special-username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-4">
                        <label for="special-password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="special-password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
                    </div>
                    <div class="mb-6">
                        <label for="secret-key" class="block text-sm font-medium text-gray-300 mb-1">Secret Key</label>
                        <input type="password" id="secret-key" name="secret_key" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" required>
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
    
    <template id="template-teacher-dashboard">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex flex-wrap justify-between items-center gap-4 mb-8">
                <h1 class="text-3xl font-bold brand-gradient">Teacher Dashboard</h1>
                <div class="flex items-center gap-2">
                    <button id="teacher-gen-code-btn" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Generate New Classroom Code</button>
                    <button id="teacher-logout-btn" class="bg-red-600 hover:bg-red-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Logout</button>
                </div>
            </header>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="glassmorphism rounded-lg p-6">
                    <h2 class="text-xl font-bold text-white mb-2">My Classroom</h2>
                    <p class="text-gray-400 mb-4">Share this code with your students so they can join your class.</p>
                    <p class="text-lg font-mono text-green-400 bg-gray-800 p-3 rounded-lg flex items-center justify-between">
                        <span id="teacher-classroom-code">Loading...</span>
                        <button id="copy-code-btn" class="text-gray-400 hover:text-white transition-colors">
                            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        </button>
                    </p>
                </div>
                <div class="glassmorphism rounded-lg p-6">
                    <h2 class="text-xl font-bold text-white mb-4">Class Leaderboard</h2>
                    <div id="teacher-leaderboard" class="space-y-2">
                        <p class="text-gray-400">No students in your class yet.</p>
                    </div>
                </div>
            </div>
            <div class="glassmorphism rounded-lg p-6 mt-8">
                <h2 class="text-xl font-bold text-white mb-4">Student Activity</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead class="border-b border-gray-600">
                            <tr>
                                <th class="p-2">Student</th>
                                <th class="p-2">Plan</th>
                                <th class="p-2">Daily Messages</th>
                                <th class="p-2">Streak</th>
                                <th class="p-2">Last Active</th>
                                <th class="p-2">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="teacher-student-list"></tbody>
                    </table>
                </div>
            </div>
            <div class="glassmorphism rounded-lg p-6 mt-8">
                <h2 class="text-xl font-bold text-white mb-4">Student Chats</h2>
                <div id="teacher-student-chats" class="space-y-4 max-h-96 overflow-y-auto">
                    <p class="text-gray-400">Select a student to view their chats.</p>
                </div>
            </div>
        </div>
    </template>

    <script>
    /****************************************************************************
     * JAVASCRIPT FRONTEND LOGIC (Myth AI - Refactored with New Features)
     ****************************************************************************/
    document.addEventListener('DOMContentLoaded', () => {
        const appState = {
            chats: {}, activeChatId: null, isAITyping: false,
            abortController: null, currentUser: null,
            isStudyMode: false, uploadedFile: null,
            teacherData: { classroom: null, students: [] },
            audio: null,
            config: { google_oauth_enabled: false, email_enabled: false }
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
                if (urlParams.get('payment') === 'success') {
                    showToast('Upgrade successful!', 'success');
                    window.history.replaceState({}, document.title, "/");
                } else if (urlParams.get('payment') === 'cancel') {
                    showToast('Payment was cancelled.', 'info');
                    window.history.replaceState({}, document.title, "/");
                }
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

        function escapeHTML(str) {
            if (typeof str !== 'string') return '';
            const p = document.createElement('p');
            p.appendChild(document.createTextNode(str));
            return p.innerHTML;
        }

        async function apiCall(endpoint, options = {}) {
            try {
                const headers = { ...(options.headers || {}) };
                if (!headers['Content-Type'] && options.body && typeof options.body === 'string') {
                    headers['Content-Type'] = 'application/json';
                }

                const response = await fetch(endpoint, { ...options, headers, credentials: 'include' });
                const data = response.headers.get("Content-Type")?.includes("application/json") ? await response.json() : null;

                if (!response.ok) {
                    if (response.status === 401 && data?.error === "Login required.") {
                        handleLogout(false);
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

        function openModal(title, bodyContent, onConfirm, confirmText = 'Confirm', confirmBtnClass = 'bg-blue-600 hover:bg-blue-500') {
            closeModal();
            const template = document.getElementById('template-modal');
            const modalWrapper = document.createElement('div');
            modalWrapper.id = 'modal-instance';
            modalWrapper.appendChild(template.content.cloneNode(true));
            DOMElements.modalContainer.appendChild(modalWrapper);
            
            modalWrapper.querySelector('#modal-title').textContent = title;
            const modalBody = modalWrapper.querySelector('#modal-body');
            modalBody.innerHTML = '';

            if (typeof bodyContent === 'string') {
                modalBody.innerHTML = `<p>${bodyContent}</p>`;
            } else {
                modalBody.appendChild(bodyContent);
            }

            if (onConfirm) {
                const confirmBtn = document.createElement('button');
                confirmBtn.className = `w-full mt-6 text-white font-bold py-2 px-4 rounded-lg ${confirmBtnClass}`;
                confirmBtn.textContent = confirmText;
                confirmBtn.onclick = () => { onConfirm(); };
                modalBody.appendChild(confirmBtn);
            }

            const doClose = () => modalWrapper.remove();
            modalWrapper.querySelector('.close-modal-btn').addEventListener('click', doClose);
            modalWrapper.querySelector('.modal-backdrop').addEventListener('click', doClose);
        }
        
        function closeModal() {
            document.getElementById('modal-instance')?.remove();
        }
        
        // --- AUTH, PASSWORD RESET, and INITIALIZATION ---
        function renderResetPasswordPage(token) {
            const template = document.getElementById('template-reset-password-page');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            renderLogo('reset-logo-container');

            document.getElementById('reset-password-form').onsubmit = async (e) => {
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
                const errorEl = body.querySelector('#forgot-error');
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
            
            if (appState.config.email_enabled) {
                document.getElementById('forgot-password-link').classList.remove('hidden');
            }
            if (appState.config.google_oauth_enabled) {
                document.getElementById('google-auth-container').classList.remove('hidden');
            }

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
                    initializeApp(result.user, result.chats, result.settings, result.config);
                } else {
                    errorEl.textContent = result.error;
                }
            };
        }
        
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
                    initializeApp(result.user, result.chats, result.settings, result.config);
                } else {
                    errorEl.textContent = result.error;
                }
            };
        }

        function renderTeacherSignupPage() {
            const template = document.getElementById('template-teacher-signup-page');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            renderLogo('teacher-signup-logo-container');
            document.getElementById('back-to-main-login').onclick = () => renderAuthPage(true);
            
            document.getElementById('teacher-signup-form').onsubmit = async (e) => {
                e.preventDefault();
                const form = e.target;
                const errorEl = document.getElementById('teacher-signup-error');
                errorEl.textContent = '';
                const formData = new FormData(form);
                const data = Object.fromEntries(formData.entries());
                
                const result = await apiCall('/api/teacher_signup', {
                    method: 'POST',
                    body: JSON.stringify(data),
                });

                if (result.success) {
                    initializeApp(result.user, result.chats, result.settings, result.config);
                } else {
                    errorEl.textContent = result.error;
                }
            };
        }

        function renderSpecialAuthPage() {
            const template = document.getElementById('template-special-auth-page');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            renderLogo('special-auth-logo-container');
            document.getElementById('back-to-main-login').onclick = () => renderAuthPage(true);
            
            document.getElementById('special-auth-form').onsubmit = async (e) => {
                e.preventDefault();
                const errorEl = document.getElementById('special-auth-error');
                errorEl.textContent = '';
                const formData = new FormData(e.target);
                const data = Object.fromEntries(formData.entries());
                const result = await apiCall('/api/special_signup', {
                    method: 'POST',
                    body: JSON.stringify(data),
                });
                if (result.success) {
                    initializeApp(result.user, {}, result.settings, result.config);
                } else {
                    errorEl.textContent = result.error;
                }
            };
        }

        async function checkLoginStatus() {
            const result = await apiCall('/api/status');
            if (result.success && result.logged_in) {
                initializeApp(result.user, result.chats, result.settings, result.config);
            } else {
                appState.config = result.config || { google_oauth_enabled: false, email_enabled: false };
                renderAuthPage();
            }
        }

        function initializeApp(user, chats, settings, config) {
            appState.currentUser = user;
            appState.chats = chats || {};
            appState.config = config || { google_oauth_enabled: false, email_enabled: false };
            if (settings && settings.announcement) {
                DOMElements.announcementBanner.textContent = settings.announcement;
                DOMElements.announcementBanner.classList.remove('hidden');
            } else {
                DOMElements.announcementBanner.classList.add('hidden');
            }

            if (user.role === 'admin') {
                renderAdminDashboard();
            } else if (user.account_type === 'teacher') {
                renderTeacherDashboard();
            } else {
                renderAppUI();
            }
        }

        function renderAppUI(){
            const template = document.getElementById('template-app-wrapper');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            renderLogo('app-logo-container');
            const sortedChatIds = Object.keys(appState.chats).sort((a, b) => (appState.chats[b].created_at || '').localeCompare(appState.chats[a].created_at || ''));
            appState.activeChatId = sortedChatIds.length > 0 ? sortedChatIds[0] : null;
            renderChatHistoryList();
            renderActiveChat();
            updateUserInfo();
            setupAppEventListeners();
            renderStudyModeToggle();
            if (appState.currentUser.account_type === 'student') {
                fetchStudentLeaderboard();
            }
        }
        async function renderActiveChat(){
            const messageList = document.getElementById('message-list');
            const chatTitle = document.getElementById('chat-title');
            if (!messageList || !chatTitle) return;

            messageList.innerHTML = '';
            appState.uploadedFile = null;
            updatePreviewContainer();
            const chat = appState.chats[appState.activeChatId];

            if (chat && chat.messages && chat.messages.length > 0) {
                chatTitle.textContent = chat.title;
                chat.messages.forEach(msg => addMessageToDOM(msg));
                renderCodeCopyButtons();
            } else {
                chatTitle.textContent = 'New Chat';
                renderWelcomeScreen();
            }
            updateUIState();
        }
        function renderWelcomeScreen(){
            const messageList = document.getElementById('message-list');
            if (!messageList) return;
            const template = document.getElementById('template-welcome-screen');
            messageList.innerHTML = '';
            messageList.appendChild(template.content.cloneNode(true));
            renderLogo('welcome-logo-container');
            if (appState.isStudyMode) {
                document.getElementById('welcome-title').textContent = "Welcome to Study Buddy!";
                document.getElementById('welcome-subtitle').textContent = "Let's learn something new. Ask me a question about your homework.";
            } else {
                document.getElementById('welcome-title').textContent = "Welcome to Myth AI";
                document.getElementById('welcome-subtitle').textContent = "How can I help you today?";
            }
        }
        function renderChatHistoryList(){
            const listEl = document.getElementById('chat-history-list');
            if (!listEl) return;
            listEl.innerHTML = '';
            Object.values(appState.chats).sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '')).forEach(chat => {
                const itemWrapper = document.createElement('div');
                itemWrapper.className = `w-full flex items-center justify-between p-3 rounded-lg hover:bg-gray-700/50 transition-colors duration-200 group ${chat.id === appState.activeChatId ? 'bg-blue-600/30' : ''}`;
                const chatButton = document.createElement('button');
                chatButton.className = 'flex-grow text-left truncate text-sm font-semibold';
                chatButton.textContent = chat.title;
                chatButton.onclick = () => {
                    appState.activeChatId = chat.id;
                    renderActiveChat();
                    renderChatHistoryList();
                    const menuToggleBtn = document.getElementById('menu-toggle-btn');
                    if (menuToggleBtn && menuToggleBtn.offsetParent !== null) {
                        document.getElementById('sidebar')?.classList.add('-translate-x-full');
                        document.getElementById('sidebar-backdrop')?.classList.add('hidden');
                    }
                };
                itemWrapper.appendChild(chatButton);
                listEl.appendChild(itemWrapper);
            });
        }
        function updateUserInfo(){
            const userInfoDiv = document.getElementById('user-info');
            if (!userInfoDiv || !appState.currentUser) return;
            const { username, plan, account_type } = appState.currentUser;
            const planDetails = PLAN_CONFIG[plan] || PLAN_CONFIG['free'];
            const avatarColor = `hsl(${username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;
            userInfoDiv.innerHTML = `<div class="flex items-center gap-3"><div class="flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center font-bold text-white" style="background-color: ${avatarColor};">${username[0].toUpperCase()}</div><div><div class="font-semibold">${username}</div><div class="text-xs ${planDetails.color}">${planDetails.name} Plan (${account_type.charAt(0).toUpperCase() + account_type.slice(1)})</div></div></div>`;
            const limitDisplay = document.getElementById('message-limit-display');
            if(limitDisplay) limitDisplay.textContent = `Daily Messages: ${appState.currentUser.daily_messages} / ${planDetails.message_limit}`;
        }
        function updateUIState(){
            const sendBtn = document.getElementById('send-btn');
            if (sendBtn) sendBtn.disabled = appState.isAITyping;
            const stopContainer = document.getElementById('stop-generating-container');
            if (stopContainer) stopContainer.style.display = appState.isAITyping ? 'block' : 'none';
            
            const chatExists = !!(appState.activeChatId && appState.chats[appState.activeChatId] && appState.chats[appState.activeChatId].messages.length > 0);
            ['share-chat-btn', 'rename-chat-btn', 'delete-chat-btn', 'download-chat-btn'].forEach(id => {
                const btn = document.getElementById(id);
                if (btn) btn.style.display = chatExists ? 'flex' : 'none';
            });
            const uploadBtn = document.getElementById('upload-btn');
            if (uploadBtn) {
                const planDetails = PLAN_CONFIG[appState.currentUser.plan] || PLAN_CONFIG['free'];
                uploadBtn.style.display = planDetails.can_upload ? 'block' : 'none';
            }
        }
        function renderStudyModeToggle(){
            const container = document.getElementById('study-mode-toggle-container');
            if (!container || appState.currentUser.account_type !== 'student') return;
            container.classList.remove('hidden');
            container.innerHTML = `<label for="study-mode-toggle" class="flex items-center cursor-pointer p-2 rounded-lg bg-yellow-900/50 border border-yellow-700"><div class="relative"><input type="checkbox" id="study-mode-toggle" class="sr-only"><div class="block bg-gray-600 w-14 h-8 rounded-full"></div><div class="dot absolute left-1 top-1 bg-white w-6 h-6 rounded-full transition"></div></div><div class="ml-3 font-medium text-yellow-300">Study Buddy Mode</div></label>`;
            const toggle = document.getElementById('study-mode-toggle');
            toggle.addEventListener('change', () => {
                appState.isStudyMode = toggle.checked;
                document.body.classList.toggle('study-buddy-mode', appState.isStudyMode);
                renderActiveChat();
            });
        }
        function updatePreviewContainer(){
            const previewContainer = document.getElementById('preview-container');
            if (!previewContainer) return;
            if (appState.uploadedFile) {
                previewContainer.classList.remove('hidden');
                const objectURL = URL.createObjectURL(appState.uploadedFile);
                previewContainer.innerHTML = `<div class="relative inline-block"><img src="${objectURL}" alt="Image preview" class="h-16 w-16 object-cover rounded-md"><button id="remove-preview-btn" class="absolute -top-2 -right-2 bg-red-600 text-white rounded-full w-5 h-5 flex items-center justify-center text-xs">&times;</button></div>`;
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
        async function handleSendMessage(){
            const userInput = document.getElementById('user-input');
            if (!userInput) return;
            const prompt = userInput.value.trim();
            if ((!prompt && !appState.uploadedFile) || appState.isAITyping) return;
            appState.isAITyping = true;
            appState.abortController = new AbortController();
            updateUIState();
            try {
                if (!appState.activeChatId) {
                    if (!await createNewChat(false)) throw new Error("Could not start chat.");
                }
                if (appState.chats[appState.activeChatId]?.messages.length === 0) {
                    document.getElementById('message-list').innerHTML = '';
                }
                addMessageToDOM({ sender: 'user', content: prompt });
                const aiContentEl = addMessageToDOM({ sender: 'model', content: '' }, true).querySelector('.message-content');
                userInput.value = '';
                userInput.style.height = 'auto';
                const formData = new FormData();
                formData.append('chat_id', appState.activeChatId);
                formData.append('prompt', prompt);
                formData.append('is_study_mode', appState.isStudyMode);
                if (appState.uploadedFile) {
                    formData.append('file', appState.uploadedFile);
                    appState.uploadedFile = null;
                    updatePreviewContainer();
                }
                const response = await fetch('/api/chat', { method: 'POST', body: formData, signal: appState.abortController.signal });
                if (!response.ok) {
                    const errorData = await response.json();
                    throw new Error(errorData.error || `Server error: ${response.status}`);
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let fullResponse = '';
                const chatWindow = document.getElementById('chat-window');
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    fullResponse += decoder.decode(value, { stream: true });
                    aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse + '<span class="animate-pulse">▍</span>'));
                    if(chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
                }
                aiContentEl.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse || "Empty response."));
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
                    showToast(err.message, 'error');
                }
            } finally {
                appState.isAITyping = false;
                appState.abortController = null;
                updateUIState();
            }
        }
        function addMessageToDOM(msg, isStreaming = false){
            const messageList = document.getElementById('message-list');
            const chatWindow = document.getElementById('chat-window');
            if (!messageList || !chatWindow || !appState.currentUser) return null;

            const wrapper = document.createElement('div');
            wrapper.className = 'message-wrapper flex items-start gap-4';
            const senderIsAI = msg.sender === 'model';
            const avatarChar = senderIsAI ? 'M' : appState.currentUser.username[0].toUpperCase();
            const userAvatarColor = `background-color: hsl(${appState.currentUser.username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;
            const aiAvatarSVG = `<svg width="20" height="20" viewBox="0 0 100 100"><path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="8" fill="none"/></svg>`;
            const userAvatarHTML = `<div class="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white" style="${userAvatarColor}">${avatarChar}</div>`;
            const aiAvatarHTML = `<div class="ai-avatar flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white bg-gradient-to-br from-blue-500 to-indigo-600">${aiAvatarSVG}</div>`;
            
            const messageContentHTML = isStreaming 
                ? '<div class="typing-indicator"><span></span><span></span><span></span></div>' 
                : DOMPurify.sanitize(marked.parse(msg.content || ""));

            wrapper.innerHTML = `${senderIsAI ? aiAvatarHTML : userAvatarHTML}<div class="flex-1 min-w-0"><div class="font-bold">${senderIsAI ? (appState.isStudyMode ? 'Study Buddy' : 'Myth AI') : 'You'}</div><div class="prose prose-invert max-w-none message-content">${messageContentHTML}</div></div>`;
            
            messageList.appendChild(wrapper);
            chatWindow.scrollTop = chatWindow.scrollHeight;
            return wrapper;
        }
        async function createNewChat(shouldRender = true){
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
        function renderCodeCopyButtons(){
            document.querySelectorAll('pre').forEach(pre => {
                if (pre.querySelector('.copy-code-btn')) return;
                const button = document.createElement('button');
                button.className = 'copy-code-btn';
                button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
                button.onclick = () => {
                    navigator.clipboard.writeText(pre.querySelector('code')?.innerText || '').then(() => {
                        button.textContent = 'Copied!';
                        setTimeout(() => button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>', 2000);
                    });
                };
                pre.appendChild(button);
            });
        }
        function setupAppEventListeners(){
            // Use event delegation for dynamically created elements within the app container.
            // This is more efficient and reliable than adding listeners to each button individually.
            DOMElements.appContainer.onclick = (e) => {
                const target = e.target.closest('button');
                if (!target) return;
                switch (target.id) {
                    case 'new-chat-btn': createNewChat(true); break;
                    case 'logout-btn': case 'teacher-logout-btn': case 'admin-logout-btn': handleLogout(); break;
                    case 'send-btn': handleSendMessage(); break;
                    case 'stop-generating-btn': appState.abortController?.abort(); break;
                    case 'rename-chat-btn': handleRenameChat(); break;
                    case 'delete-chat-btn': handleDeleteChat(); break;
                    case 'share-chat-btn': handleShareChat(); break;
                    case 'download-chat-btn': handleDownloadChat(); break;
                    case 'upgrade-plan-btn': renderUpgradePage(); break;
                    case 'back-to-chat-btn': renderAppUI(); break;
                    case 'upload-btn': document.getElementById('file-input')?.click(); break;
                    case 'menu-toggle-btn': 
                        document.getElementById('sidebar')?.classList.toggle('-translate-x-full');
                        document.getElementById('sidebar-backdrop')?.classList.toggle('hidden');
                        break;
                    case 'admin-impersonate-btn': handleImpersonate(); break;
                    case 'back-to-main-login': renderAuthPage(true); break;
                    case 'teacher-gen-code-btn': handleGenerateClassroomCode(); break;
                    case 'copy-code-btn': handleCopyClassroomCode(); break;
                }
                if (target.classList.contains('delete-user-btn')) handleAdminDeleteUser(target.dataset.userid, target.dataset.username);
                if (target.classList.contains('purchase-btn') && !target.disabled) handlePurchase(target.dataset.planid);
                if (target.classList.contains('view-student-chats-btn')) handleViewStudentChats(target.dataset.userid, target.dataset.username);
                if (target.classList.contains('kick-student-btn')) handleKickStudent(target.dataset.userid, target.dataset.username);
            };
            const userInput = document.getElementById('user-input');
            if (userInput) {
                userInput.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); } };
                userInput.oninput = () => { userInput.style.height = 'auto'; userInput.style.height = `${userInput.scrollHeight}px`; };
            }
            const backdrop = document.getElementById('sidebar-backdrop');
            if (backdrop) backdrop.onclick = () => {
                document.getElementById('sidebar')?.classList.add('-translate-x-full');
                backdrop.classList.add('hidden');
            };
            const fileInput = document.getElementById('file-input');
            if (fileInput) fileInput.onchange = (e) => {
                if (e.target.files.length > 0) {
                    const planDetails = PLAN_CONFIG[appState.currentUser.plan] || PLAN_CONFIG['free'];
                    if (!planDetails.can_upload) {
                        showToast("Your current plan does not support image uploads.", "error");
                        e.target.value = null;
                        return;
                    }
                    appState.uploadedFile = e.target.files[0];
                    updatePreviewContainer();
                }
            };
            const announcementForm = document.getElementById('announcement-form');
            if(announcementForm) announcementForm.onsubmit = handleSetAnnouncement;
        }
        async function handleLogout(doApiCall = true){
            if(doApiCall) await apiCall('/api/logout');
            appState.currentUser = null;
            appState.chats = {};
            appState.activeChatId = null;
            DOMElements.announcementBanner.classList.add('hidden');
            renderAuthPage();
        }
        function handleRenameChat(){
            if (!appState.activeChatId) return;
            const oldTitle = appState.chats[appState.activeChatId].title;

            const body = document.createElement('div');
            body.innerHTML = `
                <p class="mb-4 text-gray-400">Enter a new name for this chat.</p>
                <input type="text" id="modal-input" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" value="${escapeHTML(oldTitle)}">
                <p id="modal-error" class="text-red-400 text-sm h-4 mt-2"></p>
            `;

            openModal('Rename Chat', body, () => {
                const input = document.getElementById('modal-input');
                const newTitle = input.value.trim();
                const errorEl = document.getElementById('modal-error');
                if (!newTitle) {
                    errorEl.textContent = 'Title cannot be empty.';
                    return;
                }
                if (newTitle === oldTitle) {
                    closeModal();
                    return;
                }

                apiCall('/api/chat/rename', {
                    method: 'POST',
                    body: JSON.stringify({ chat_id: appState.activeChatId, title: newTitle }),
                }).then(result => {
                    if (result.success) {
                        appState.chats[appState.activeChatId].title = newTitle;
                        renderChatHistoryList();
                        document.getElementById('chat-title').textContent = newTitle;
                        showToast("Chat renamed!", "success");
                        closeModal();
                    } else {
                        errorEl.textContent = result.error || 'Failed to rename chat.';
                    }
                });
            }, 'Save');
        }
        function handleDeleteChat(){
            if (!appState.activeChatId) return;
            const chatTitle = appState.chats[appState.activeChatId].title;
            const bodyContent = `Are you sure you want to delete the chat "<strong>${escapeHTML(chatTitle)}</strong>"? This action cannot be undone.`;

            openModal('Delete Chat', bodyContent, () => {
                apiCall('/api/chat/delete', {
                    method: 'POST',
                    body: JSON.stringify({ chat_id: appState.activeChatId }),
                }).then(result => {
                    if (result.success) {
                        delete appState.chats[appState.activeChatId];
                        const sortedChatIds = Object.keys(appState.chats).sort((a, b) => (appState.chats[b].created_at || '').localeCompare(appState.chats[a].created_at || ''));
                        appState.activeChatId = sortedChatIds.length > 0 ? sortedChatIds[0] : null;
                        renderChatHistoryList();
                        renderActiveChat();
                        showToast("Chat deleted.", "success");
                    } else {
                        showToast(result.error || 'Failed to delete chat.', 'error');
                    }
                    closeModal();
                });
            }, 'Delete', 'bg-red-600 hover:bg-red-500');
        }
        async function handleShareChat(){
            if (!appState.activeChatId) return;
            const result = await apiCall('/api/chat/share', {
                method: 'POST',
                body: JSON.stringify({ chat_id: appState.activeChatId }),
            });
            if (result.success) {
                const shareUrl = `${window.location.origin}/share/${result.share_id}`;
                const body = document.createElement('div');
                body.innerHTML = `
                    <p class="mb-4 text-gray-400">Anyone with this link can view the chat.</p>
                    <input type="text" id="modal-input" class="w-full p-2 bg-gray-800 rounded-lg border border-gray-600" value="${escapeHTML(shareUrl)}" readonly>
                `;
                openModal('Shareable Link', body, () => {
                    navigator.clipboard.writeText(shareUrl);
                    showToast('Link copied to clipboard!', 'success');
                }, 'Copy Link');
            }
        }
        async function handleDownloadChat(){
            if (!appState.activeChatId) return;
            const chat = appState.chats[appState.activeChatId];
            if (!chat || chat.messages.length === 0) {
                showToast("No chat content to download.", "info");
                return;
            }
            let content = `# ${chat.title}\n\n`;
            chat.messages.forEach(msg => {
                const sender = msg.sender === 'user' ? (appState.currentUser.username || 'You') : 'Myth AI';
                content += `**${sender}:**\n${msg.content}\n\n---\n\n`;
            });
            const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `${chat.title.replace(/[^a-z0-9]/gi, '_')}_chat.md`;
            link.click();
            link.remove();
            showToast("Chat downloaded!", "success");
        }
        async function renderUpgradePage(){
            const template = document.getElementById('template-upgrade-page');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            setupAppEventListeners();
            const plansContainer = document.getElementById('plans-container');
            const plansResult = await apiCall('/api/plans');
            if (plansResult.success) {
                const { plans, user_plan } = plansResult;
                Object.keys(plans).forEach(planId => {
                    const plan = plans[planId];
                    const card = document.createElement('div');
                    const isCurrent = planId === user_plan;
                    card.className = `p-8 glassmorphism rounded-lg border-2 ${isCurrent ? 'border-green-500' : 'border-gray-600'}`;
                    card.innerHTML = `<h2 class="text-2xl font-bold text-center ${plan.color}">${plan.name}</h2><p class="text-4xl font-bold text-center my-4 text-white">${plan.price_string}</p><ul class="space-y-2 text-gray-300 mb-6">${plan.features.map(f => `<li>✓ ${f}</li>`).join('')}</ul><button ${isCurrent || planId === 'free' ? 'disabled' : ''} data-planid="${planId}" class="purchase-btn w-full mt-6 font-bold py-3 px-4 rounded-lg transition-opacity ${isCurrent ? 'bg-gray-600 cursor-not-allowed' : 'bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90'}">${isCurrent ? 'Current Plan' : 'Upgrade'}</button>`;
                    plansContainer.appendChild(card);
                });
            }
        }
        async function handlePurchase(planId){
            const config = await apiCall('/api/config');
            if (config.success && config.stripe_public_key) {
                const stripe = Stripe(config.stripe_public_key);
                const sessionResult = await apiCall('/api/create-checkout-session', {
                    method: 'POST',
                    body: JSON.stringify({ plan_id: planId })
                });
                if (sessionResult.success) {
                    stripe.redirectToCheckout({ sessionId: sessionResult.id });
                }
            }
        }
        function renderAdminDashboard(){
            const template = document.getElementById('template-admin-dashboard');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            renderLogo('admin-logo-container');
            setupAppEventListeners();
            fetchAdminData();
        }
        async function renderTeacherDashboard(){
            const template = document.getElementById('template-teacher-dashboard');
            DOMElements.appContainer.innerHTML = '';
            DOMElements.appContainer.appendChild(template.content.cloneNode(true));
            setupAppEventListeners();
            await fetchTeacherData();
        }
        async function fetchStudentLeaderboard(){
            const leaderboardContainer = document.getElementById('student-leaderboard-container');
            if (!leaderboardContainer) return;
            const result = await apiCall('/api/student/leaderboard');
            if (result.success) {
                leaderboardContainer.classList.remove('hidden');
                let html = `<h3 class="text-lg font-bold mb-2">Class Leaderboard</h3>`;
                if (result.leaderboard.length > 0) {
                    html += `<ul class="space-y-1">${result.leaderboard.map((s, i) => `<li class="flex justify-between items-center text-sm"><span class="truncate"><strong>${i + 1}.</strong> ${s.username}</span><span class="font-mono text-yellow-400">${s.streak} days</span></li>`).join('')}</ul>`;
                } else {
                    html += `<p class="text-sm text-gray-400">No students have a streak yet.</p>`;
                }
                leaderboardContainer.innerHTML = html;
            }
        }
        async function fetchAdminData(){
            const data = await apiCall('/api/admin_data');
            if (!data.success) return;
            document.getElementById('admin-total-users').textContent = data.stats.total_users;
            document.getElementById('admin-pro-users').textContent = data.stats.pro_users;
            document.getElementById('admin-ultra-users').textContent = data.stats.ultra_users;
            document.getElementById('announcement-input').value = data.announcement;
            const userList = document.getElementById('admin-user-list');
            userList.innerHTML = '';
            data.users.forEach(user => {
                const tr = document.createElement('tr');
                tr.className = 'border-b border-gray-700/50';
                tr.innerHTML = `<td class="p-2">${user.username}</td><td class="p-2">${user.email || 'N/A'}</td><td class="p-2">${user.role}</td><td class="p-2">${user.plan}</td><td class="p-2 flex gap-2"><button data-userid="${user.id}" data-username="${user.username}" class="delete-user-btn text-xs px-2 py-1 rounded bg-red-600">Delete</button></td>`;
                userList.appendChild(tr);
            });
        }
        async function fetchTeacherData(){
            const data = await apiCall('/api/teacher/dashboard_data');
            if (!data.success) return;
            const { classroom, students } = data;
            appState.teacherData = { classroom, students };
            document.getElementById('teacher-classroom-code').textContent = classroom.code || 'None';
            const studentListEl = document.getElementById('teacher-student-list');
            studentListEl.innerHTML = '';
            students.forEach(s => {
                const tr = document.createElement('tr');
                tr.className = 'border-b border-gray-700/50';
                tr.innerHTML = `<td class="p-2">${s.username}</td><td class="p-2">${s.plan}</td><td class="p-2">${s.daily_messages}/${s.message_limit}</td><td class="p-2">${s.streak} days</td><td class="p-2">${s.last_message_date}</td><td class="p-2 flex gap-2"><button data-userid="${s.id}" data-username="${s.username}" class="view-student-chats-btn text-xs px-2 py-1 rounded bg-blue-600">View</button><button data-userid="${s.id}" data-username="${s.username}" class="kick-student-btn text-xs px-2 py-1 rounded bg-red-600">Kick</button></td>`;
                studentListEl.appendChild(tr);
            });
            const leaderboardEl = document.getElementById('teacher-leaderboard');
            if (students.length > 0) {
                const sorted = [...students].sort((a, b) => b.streak - a.streak);
                leaderboardEl.innerHTML = `<ul class="space-y-2">${sorted.map((s, i) => `<li class="flex justify-between"><span>${i + 1}. ${s.username}</span><span class="font-bold text-yellow-400">${s.streak} days</span></li>`).join('')}</ul>`;
            } else {
                leaderboardEl.innerHTML = `<p class="text-gray-400">No students yet.</p>`;
            }
        }
        async function handleGenerateClassroomCode(){
            const result = await apiCall('/api/teacher/generate_classroom_code', { method: 'POST' });
            if (result.success) {
                showToast('New classroom code generated!', 'success');
                await fetchTeacherData();
            }
        }
        function handleCopyClassroomCode(){
            const code = document.getElementById('teacher-classroom-code').textContent;
            if (code && code !== 'None' && code !== 'Loading...') {
                navigator.clipboard.writeText(code);
                showToast('Classroom code copied!', 'success');
            }
        }
        async function handleViewStudentChats(studentId, studentUsername){
            const result = await apiCall(`/api/teacher/student_chats/${studentId}`);
            if (result.success) {
                const container = document.getElementById('teacher-student-chats');
                container.innerHTML = `<h3 class="text-lg font-bold text-white mb-2">Chat History for ${escapeHTML(studentUsername)}</h3>`;
                if (result.chats.length > 0) {
                    result.chats.forEach(chat => {
                        const el = document.createElement('div');
                        el.className = 'bg-gray-800 p-4 rounded-lg border border-gray-700';
                        let messagesHTML = '';
                        chat.messages.forEach(msg => {
                            messagesHTML += `<div class="p-2 mt-2 rounded-lg text-sm ${msg.sender === 'user' ? 'bg-blue-900/30' : 'bg-gray-700/30'}"><strong>${msg.sender === 'user' ? 'Student' : 'AI'}:</strong> ${DOMPurify.sanitize(msg.content)}</div>`;
                        });
                        el.innerHTML = `<h4 class="font-semibold">${escapeHTML(chat.title)}</h4>${messagesHTML}`;
                        container.appendChild(el);
                    });
                } else {
                    container.innerHTML += '<p class="text-gray-400">This student has no chat history.</p>';
                }
            }
        }
        async function handleKickStudent(studentId, studentUsername){
            const bodyContent = `Are you sure you want to kick the student "<strong>${escapeHTML(studentUsername)}</strong>" from your classroom?`;
            openModal('Kick Student', bodyContent, async () => {
                const result = await apiCall('/api/teacher/kick_student', {
                    method: 'POST',
                    body: JSON.stringify({ student_id: studentId }),
                });
                if (result.success) {
                    showToast(result.message, 'success');
                    await fetchTeacherData();
                }
                closeModal();
            }, 'Kick Student', 'bg-red-600 hover:bg-red-500');
        }
        async function handleSetAnnouncement(e){
            e.preventDefault();
            const text = document.getElementById('announcement-input').value;
            const result = await apiCall('/api/admin/announcement', {
                method: 'POST',
                body: JSON.stringify({ text }),
            });
            if (result.success) {
                showToast(result.message, 'success');
                DOMElements.announcementBanner.textContent = text;
                DOMElements.announcementBanner.classList.toggle('hidden', !text);
            }
        }
        function handleAdminDeleteUser(userId, username){
            const bodyContent = `Are you sure you want to delete user "<strong>${escapeHTML(username)}</strong>"? This is irreversible.`;
            openModal('Delete User', bodyContent, () => {
                apiCall('/api/admin/delete_user', {
                    method: 'POST',
                    body: JSON.stringify({ user_id: userId }),
                }).then(result => {
                    if (result.success) {
                        showToast(result.message, 'success');
                        fetchAdminData();
                    }
                    closeModal();
                });
            }, 'Delete User', 'bg-red-600 hover:bg-red-500');
        }
        async function handleImpersonate(){
            const body = document.createElement('div');
            body.innerHTML = `
                <p class="mb-4 text-gray-400">Enter the username of the user you want to impersonate.</p>
                <input type="text" id="modal-input" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600" placeholder="Username">
                <p id="modal-error" class="text-red-400 text-sm h-4 mt-2"></p>
            `;
            openModal('Impersonate User', body, async () => {
                const username = document.getElementById('modal-input').value;
                const errorEl = document.getElementById('modal-error');
                if (!username) {
                    errorEl.textContent = 'Username is required.';
                    return;
                }
                const result = await apiCall('/api/admin/impersonate', {
                    method: 'POST',
                    body: JSON.stringify({ username }),
                });
                if (result.success) {
                    showToast(`Now impersonating ${username}. Reloading...`, 'success');
                    setTimeout(() => window.location.reload(), 1500);
                } else {
                    errorEl.textContent = result.error;
                }
            }, 'Impersonate');
        }

        // --- INITIAL LOAD ---
        routeHandler();
    });
    </script>
</body>
</html>
"""

# --- 7. Backend Helper Functions ---

def send_password_reset_email(user):
    """Generates a reset token and sends the email."""
    if not EMAIL_ENABLED:
        logging.error("Attempted to send email, but mail is not configured.")
        return False
    try:
        token = password_reset_serializer.dumps(user.email, salt='password-reset-salt')
        reset_url = url_for('index', _external=True) + f"reset-password/{token}"
        
        msg_body = f"Hello {user.username},\n\nPlease click the following link to reset your password:\n{reset_url}\n\nThis link will expire in one hour. If you did not request this, please ignore this email."
        msg = MIMEText(msg_body)
        msg['Subject'] = 'Password Reset Request for Myth AI'
        msg['From'] = SITE_CONFIG['MAIL_SENDER']
        msg['To'] = user.email

        with smtplib.SMTP(SITE_CONFIG['MAIL_SERVER'], SITE_CONFIG['MAIL_PORT']) as server:
            if SITE_CONFIG['MAIL_USE_TLS']:
                server.starttls()
            server.login(SITE_CONFIG['MAIL_USERNAME'], SITE_CONFIG['MAIL_PASSWORD'])
            server.send_message(msg)
        logging.info(f"Password reset email sent to {user.email}")
        return True
    except Exception as e:
        logging.error(f"Failed to send password reset email to {user.email}: {e}")
        return False

def check_and_reset_daily_limit(user):
    """Resets a user's daily message count and checks streak."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0
        if user.account_type == 'student':
            try:
                last_streak_date = datetime.strptime(user.last_streak_date, "%Y-%m-%d")
                if (datetime.now().date() - last_streak_date.date()).days > 1:
                    user.streak = 0
            except (ValueError, TypeError):
                user.streak = 0
        save_database()

def get_user_data_for_frontend(user):
    """Prepares user data for sending to the frontend."""
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_details = PLAN_CONFIG.get(user.plan, PLAN_CONFIG['free'])
    return {
        "id": user.id, "username": user.username, "email": user.email, "role": user.role, "plan": user.plan,
        "account_type": user.account_type, "daily_messages": user.daily_messages,
        "message_limit": plan_details["message_limit"], "can_upload": plan_details["can_upload"],
        "is_student_in_class": user.account_type == 'student' and user.classroom_code is not None,
        "streak": user.streak,
    }

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

@app.route('/api/config')
@login_required
def get_config():
    return jsonify({"stripe_public_key": SITE_CONFIG["STRIPE_PUBLIC_KEY"]})

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
        
    new_user = User(id=username, username=username, email=email, password_hash=generate_password_hash(password), account_type='general')
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user),
        "chats": {}, "settings": DB['site_settings'],
        "config": {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
    })

@app.route('/api/student_signup', methods=['POST'])
@rate_limited()
def student_signup():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    email = data.get('email', '').strip().lower()
    classroom_code = data.get('classroom_code', '').strip().upper()

    if not all([username, password, email, classroom_code]) or len(username) < 3 or len(password) < 6 or '@' not in email:
        return jsonify({"error": "All fields are required."}), 400
    if User.get_by_username(username): return jsonify({"error": "Username already exists."}), 409
    if User.get_by_email(email): return jsonify({"error": "Email already in use."}), 409
    if classroom_code not in DB['classrooms']: return jsonify({"error": "Invalid classroom code."}), 403

    new_user = User(id=username, username=username, email=email, password_hash=generate_password_hash(password), account_type='student', plan='student', classroom_code=classroom_code)
    DB['users'][new_user.id] = new_user
    DB['classrooms'][classroom_code]['students'].append(new_user.id)
    save_database()
    login_user(new_user, remember=True)
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user),
        "chats": {}, "settings": DB['site_settings'],
        "config": {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
    })

@app.route('/api/teacher_signup', methods=['POST'])
@rate_limited()
def teacher_signup():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    email = data.get('email', '').strip().lower()
    secret_key = data.get('secret_key')

    if secret_key != SITE_CONFIG["SECRET_TEACHER_KEY"]: return jsonify({"error": "Invalid teacher access key."}), 403
    if not all([username, password, email]) or len(username) < 3 or len(password) < 6 or '@' not in email:
        return jsonify({"error": "All fields are required."}), 400
    if User.get_by_username(username): return jsonify({"error": "Username already exists."}), 409
    if User.get_by_email(email): return jsonify({"error": "Email already in use."}), 409

    new_user = User(id=username, username=username, email=email, password_hash=generate_password_hash(password), account_type='teacher', plan='pro')
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user),
        "chats": {}, "settings": DB['site_settings'],
        "config": {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
    })

@app.route('/api/login', methods=['POST'])
@rate_limited()
def login():
    data = request.get_json()
    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    
    if user and user.password_hash and check_password_hash(user.password_hash, password):
        login_user(user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id) if user.role not in ['admin', 'teacher'] else {},
            "settings": DB['site_settings'],
            "config": {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
        })
    return jsonify({"error": "Invalid username or password."}), 401
    
@app.route('/api/login/google')
def google_login():
    if not GOOGLE_OAUTH_ENABLED:
        return "Google Sign-In is not configured on this server.", 404
    redirect_uri = url_for('authorize', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    if not GOOGLE_OAUTH_ENABLED:
        return "Google Sign-In is not configured on this server.", 404
    try:
        token = oauth.google.authorize_access_token()
        user_info = oauth.google.get('openid/userinfo').json()
    except Exception as e:
        logging.error(f"Google OAuth failed: {e}")
        return redirect(url_for('index'))

    email = user_info.get('email', '').lower()
    if not email: return redirect(url_for('index'))

    user = User.get_by_email(email)
    if not user:
        username = user_info.get('name', email.split('@')[0])
        base_username = username
        while User.get_by_username(username):
            username = f"{base_username}{secrets.token_hex(2)}"
        user = User(id=username, username=username, email=email, password_hash=None, account_type='general', plan='free')
        DB['users'][user.id] = user
        save_database()
    
    login_user(user, remember=True)
    return redirect(url_for('index'))

@app.route('/api/request-password-reset', methods=['POST'])
@rate_limited()
def request_password_reset():
    if not EMAIL_ENABLED:
        return jsonify({"error": "Password reset is not configured on this server."}), 501
    email = request.json.get('email', '').lower()
    user = User.get_by_email(email)
    if user:
        send_password_reset_email(user)
    # Always return success to prevent user enumeration.
    return jsonify({"success": True, "message": "If an account with that email exists, a reset link has been sent."})

@app.route('/api/reset-with-token', methods=['POST'])
@rate_limited()
def reset_with_token():
    if not EMAIL_ENABLED:
        return jsonify({"error": "Password reset is not configured on this server."}), 501
    token = request.json.get('token')
    password = request.json.get('password')
    try:
        email = password_reset_serializer.loads(token, salt='password-reset-salt', max_age=3600)
    except Exception:
        return jsonify({"error": "The password reset link is invalid or has expired."}), 400

    user = User.get_by_email(email)
    if not user: return jsonify({"error": "User not found."}), 404
        
    user.password_hash = generate_password_hash(password)
    save_database()
    return jsonify({"success": True, "message": "Password has been updated."})

@app.route('/api/logout')
def logout():
    if 'impersonator_id' in session:
        impersonator = User.get(session['impersonator_id'])
        if impersonator:
            logout_user()
            login_user(impersonator)
            session.pop('impersonator_id', None)
            return redirect(url_for('index'))
    logout_user()
    return jsonify({"success": True})

@app.route('/api/status')
def status():
    config = {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
    if current_user.is_authenticated:
        return jsonify({
            "logged_in": True, "user": get_user_data_for_frontend(current_user),
            "chats": get_all_user_chats(current_user.id) if current_user.role not in ['admin', 'teacher'] else {},
            "settings": DB['site_settings'],
            "config": config
        })
    return jsonify({"logged_in": False, "config": config})

@app.route('/api/special_signup', methods=['POST'])
@rate_limited()
def special_signup():
    data = request.get_json()
    username, password, secret_key = data.get('username'), data.get('password'), data.get('secret_key')
    if secret_key != SITE_CONFIG["SECRET_REGISTRATION_KEY"]: return jsonify({"error": "Invalid secret key."}), 403
    if not all([username, password]): return jsonify({"error": "Username and password are required."}), 400
    if User.get_by_username(username): return jsonify({"error": "Username already exists."}), 409
    
    admin_email = f"{username}@example.com" # Placeholder email for admin
    new_user = User(id=username, username=username, email=admin_email, password_hash=generate_password_hash(password), role='admin', plan='ultra')
    DB['users'][new_user.id] = new_user
    save_database()
    login_user(new_user, remember=True)
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user), 
        "settings": DB['site_settings'],
        "config": {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "email_enabled": EMAIL_ENABLED}
    })


# --- 9. Chat API Routes ---
@app.route('/api/chat', methods=['POST'])
@login_required
@rate_limited(max_attempts=20)
def chat_api():
    if not GEMINI_API_CONFIGURED: return jsonify({"error": "AI services are currently unavailable."}), 503
    
    data = request.form
    chat_id = data.get('chat_id')
    prompt = data.get('prompt', '').strip()
    is_study_mode = data.get('is_study_mode') == 'true'
    
    if not chat_id: return jsonify({"error": "Missing chat identifier."}), 400
    chat = DB['chats'].get(chat_id)
    if not chat or chat.get('user_id') != current_user.id: return jsonify({"error": "Chat not found or access denied."}), 404

    check_and_reset_daily_limit(current_user)
    plan_details = PLAN_CONFIG.get(current_user.plan, PLAN_CONFIG['free'])
    if current_user.daily_messages >= plan_details["message_limit"]:
        return jsonify({"error": f"Daily message limit of {plan_details['message_limit']} reached."}), 429
    
    system_instruction = "You are Myth AI, a helpful assistant." # Simplified for brevity
    history = [{"role": "user" if msg['sender'] == 'user' else 'model', "parts": [{"text": msg['content']}]} for msg in chat['messages'][-10:] if msg.get('content')]
    
    model_input_parts = []
    if prompt: model_input_parts.append({"text": prompt})

    uploaded_file = request.files.get('file')
    if uploaded_file:
        if not plan_details['can_upload']: return jsonify({"error": "Your plan does not support file uploads."}), 403
        try:
            img = Image.open(uploaded_file.stream)
            img.thumbnail((512, 512))
            buffered = BytesIO()
            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
            img.save(buffered, format="JPEG")
            img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            model_input_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_base64}})
        except Exception as e:
            logging.error(f"Image processing error: {e}")
            return jsonify({"error": "Invalid image file."}), 400

    if not model_input_parts: return jsonify({"error": "A prompt or file is required."}), 400

    chat['messages'].append({'sender': 'user', 'content': prompt})
    current_user.daily_messages += 1
    # No need to save here, will be saved after AI response.

    model = genai.GenerativeModel(plan_details['model'], system_instruction=system_instruction)
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
            logging.error(f"Gemini stream error: {e}")
            yield json.dumps({"error": str(e)})
            return

        chat['messages'].append({'sender': 'model', 'content': full_response_text})
        if len(chat['messages']) <= 2 and prompt:
            try:
                title_prompt = f"Summarize with a short title (4 words max): User: \"{prompt}\" Assistant: \"{full_response_text[:100]}\""
                title_response = genai.GenerativeModel('gemini-1.5-flash-latest').generate_content(title_prompt)
                chat['title'] = title_response.text.strip().replace('"', '')
            except Exception as title_e:
                logging.error(f"Title generation error: {title_e}")
                chat['title'] = prompt[:40] + '...'
        save_database()

    return Response(stream_with_context(generate_chunks()), mimetype='text/plain')

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
    chat_id, new_title = data.get('chat_id'), data.get('title', '').strip()
    if not all([chat_id, new_title]): return jsonify({"error": "Chat ID and title required."}), 400
    chat = DB['chats'].get(chat_id)
    if chat and chat.get('user_id') == current_user.id:
        chat['title'] = new_title
        save_database()
        return jsonify({"success": True})
    return jsonify({"error": "Chat not found or access denied."}), 404

@app.route('/api/chat/delete', methods=['POST'])
@login_required
def delete_chat():
    chat_id = request.json.get('chat_id')
    chat = DB['chats'].get(chat_id)
    if chat and chat.get('user_id') == current_user.id:
        del DB['chats'][chat_id]
        save_database()
        return jsonify({"success": True})
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

@app.route('/api/tts', methods=['POST'])
@login_required
def tts_api():
    if not (current_user.plan in ['pro', 'ultra']): return jsonify({"error": "Premium feature."}), 403
    text = request.json.get('text', '').strip()
    if not text: return jsonify({"error": "No text provided."}), 400
    try:
        model = genai.GenerativeModel('gemini-2.5-flash-preview-tts')
        response = model.generate_content(text, generation_config=genai.GenerationConfig(response_modality="AUDIO"))
        audio_data = response.candidates[0].content.parts[0].inline_data.data
        return jsonify({"success": True, "audio_data": audio_data})
    except Exception as e:
        logging.error(f"TTS API error: {e}")
        return jsonify({"error": "Failed to generate audio."}), 500

# --- 10. Public Share and Payment Routes ---
@app.route('/share/<chat_id>')
def view_shared_chat(chat_id):
    chat = DB['chats'].get(chat_id)
    if not chat or not chat.get('is_public'): return "Chat not found or is not public.", 404
    # Simple HTML rendering for shared chat
    return f"<h1>{chat['title']}</h1>" + "".join([f"<p><b>{msg['sender']}:</b> {msg['content']}</p>" for msg in chat['messages']])

@app.route('/api/plans')
@login_required
def get_plans():
    return jsonify({
        "success": True, 
        "plans": {pid: {"name": d["name"], "price_string": d["price_string"], "features": d["features"], "color": d["color"]} for pid, d in PLAN_CONFIG.items()},
        "user_plan": current_user.plan
    })

@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if not stripe.api_key: return jsonify(error={'message': 'Payment services unavailable.'}), 500
    plan_id = request.json.get('plan_id')
    price_map = {
        "pro": {"id": SITE_CONFIG["STRIPE_PRO_PRICE_ID"], "mode": "subscription"},
        "ultra": {"id": SITE_CONFIG["STRIPE_ULTRA_PRICE_ID"], "mode": "payment"},
        "student": {"id": SITE_CONFIG["STRIPE_STUDENT_PRICE_ID"], "mode": "subscription"}
    }
    if plan_id not in price_map: return jsonify(error={'message': 'Invalid plan.'}), 400
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
        logging.error(f"Stripe session error: {e}")
        return jsonify(error={'message': "Could not create payment session."}), 500

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = SITE_CONFIG['STRIPE_WEBHOOK_SECRET']
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logging.warning(f"Stripe webhook error: {e}")
        return 'Invalid webhook signature', 400

    event_type = event['type']
    data_object = event['data']['object']
    logging.info(f"Received Stripe event: {event_type}")

    if event_type == 'checkout.session.completed':
        user = User.get(data_object.get('client_reference_id'))
        if user:
            line_item = stripe.checkout.Session.list_line_items(data_object.id, limit=1).data[0]
            price_id = line_item.price.id
            new_plan = next((p for p, d in SITE_CONFIG.items() if d == price_id), None)
            if new_plan:
                user.plan = new_plan.replace('STRIPE_', '').replace('_PRICE_ID', '').lower()
                save_database()
                logging.info(f"User {user.id} upgraded to {user.plan}")
    elif event_type == 'customer.subscription.deleted':
        customer = stripe.Customer.retrieve(data_object.customer)
        user = User.get_by_email(customer.email)
        if user and user.plan != 'ultra':
            user.plan = 'free'
            save_database()
            logging.info(f"User {user.id} subscription ended; downgraded to free.")
    return 'Success', 200


# --- 11. Admin & Teacher Routes ---
@app.route('/api/admin_data')
@admin_required
def admin_data():
    stats = {"total_users": 0, "pro_users": 0, "ultra_users": 0}
    all_users_data = []
    for user in DB["users"].values():
        if user.role != 'admin':
            stats['total_users'] += 1
            if user.plan == 'pro': stats['pro_users'] += 1
            elif user.plan == 'ultra': stats['ultra_users'] += 1
            all_users_data.append({"id": user.id, "username": user.username, "email": user.email, "plan": user.plan, "role": user.role, "account_type": user.account_type})
    return jsonify({"success": True, "stats": stats, "users": sorted(all_users_data, key=lambda x: x['username']), "announcement": DB['site_settings']['announcement']})

@app.route('/api/admin/delete_user', methods=['POST'])
@admin_required
def admin_delete_user():
    user_id = request.json.get('user_id')
    if user_id == current_user.id: return jsonify({"error": "Cannot delete yourself."}), 400
    user_to_delete = User.get(user_id)
    if not user_to_delete: return jsonify({"error": "User not found."}), 404
    if user_to_delete.role == 'admin' and len([u for u in DB['users'].values() if u.role == 'admin']) <= 1:
        return jsonify({"error": "Cannot delete the last admin."}), 403
    del DB['users'][user_id]
    for cid in [cid for cid, c in DB['chats'].items() if c.get('user_id') == user_id]: del DB['chats'][cid]
    save_database()
    return jsonify({"success": True, "message": f"User {user_id} deleted."})

@app.route('/api/admin/announcement', methods=['POST'])
@admin_required
def set_announcement():
    DB['site_settings']['announcement'] = request.json.get('text', '').strip()
    save_database()
    return jsonify({"success": True, "message": "Announcement updated."})

@app.route('/api/admin/impersonate', methods=['POST'])
@admin_required
def impersonate_user():
    username = request.json.get('username')
    user_to_impersonate = User.get_by_username(username)
    if not user_to_impersonate: return jsonify({"error": "User not found."}), 404
    if user_to_impersonate.role == 'admin': return jsonify({"error": "Cannot impersonate another admin."}), 403
    session['impersonator_id'] = current_user.id
    logout_user()
    login_user(user_to_impersonate, remember=True)
    return jsonify({"success": True})

@app.route('/api/teacher/dashboard_data', methods=['GET'])
@teacher_required
def teacher_dashboard_data():
    classroom_code = next((code for code, data in DB['classrooms'].items() if data.get('teacher_id') == current_user.id), None)
    if not classroom_code: return jsonify({"success": True, "classroom": {"code": None}, "students": []})
    student_ids = DB['classrooms'][classroom_code]['students']
    students_data = [get_user_data_for_frontend(User.get(sid)) for sid in student_ids if User.get(sid)]
    return jsonify({"success": True, "classroom": {"code": classroom_code}, "students": sorted(students_data, key=lambda x: x['streak'], reverse=True)})

@app.route('/api/teacher/generate_classroom_code', methods=['POST'])
@teacher_required
def generate_classroom_code_api():
    if any(c['teacher_id'] == current_user.id for c in DB['classrooms'].values()):
        return jsonify({"error": "You already have a classroom."}), 409
    new_code = generate_unique_classroom_code()
    DB['classrooms'][new_code] = {"teacher_id": current_user.id, "students": [], "created_at": datetime.now().isoformat()}
    save_database()
    return jsonify({"success": True, "code": new_code})

@app.route('/api/teacher/kick_student', methods=['POST'])
@teacher_required
def kick_student():
    student_id = request.json.get('student_id')
    student = User.get(student_id)
    if not student or student.account_type != 'student': return jsonify({"error": "Student not found."}), 404
    if not student.classroom_code or DB['classrooms'].get(student.classroom_code, {}).get('teacher_id') != current_user.id:
        return jsonify({"error": "Unauthorized."}), 403
    DB['classrooms'][student.classroom_code]['students'].remove(student.id)
    student.classroom_code = None
    student.streak = 0
    save_database()
    return jsonify({"success": True, "message": f"Student {student.username} kicked."})

@app.route('/api/teacher/student_chats/<student_id>', methods=['GET'])
@teacher_required
def get_student_chats(student_id):
    student = User.get(student_id)
    if not student or student.account_type != 'student': return jsonify({"error": "Student not found."}), 404
    if not student.classroom_code or DB['classrooms'].get(student.classroom_code, {}).get('teacher_id') != current_user.id:
        return jsonify({"error": "Unauthorized."}), 403
    student_chats = list(get_all_user_chats(student_id).values())
    return jsonify({"success": True, "chats": sorted(student_chats, key=lambda c: c.get('created_at'), reverse=True)})
    
@app.route('/api/student/leaderboard', methods=['GET'])
@login_required
def student_leaderboard_data():
    if current_user.account_type != 'student' or not current_user.classroom_code:
        return jsonify({"success": False, "error": "Not in a classroom."}), 403
    student_ids = DB['classrooms'][current_user.classroom_code]['students']
    students_data = [get_user_data_for_frontend(User.get(sid)) for sid in student_ids if User.get(sid)]
    return jsonify({"success": True, "leaderboard": sorted(students_data, key=lambda x: x['streak'], reverse=True)})


# --- Main Execution ---
if __name__ == '__main__':
    # Use the PORT environment variable if available, for compatibility with hosting platforms.
    port = int(os.environ.get('PORT', 5000))
    # Set debug=False for production environments.
    app.run(host='0.0.0.0', port=port, debug=True)
