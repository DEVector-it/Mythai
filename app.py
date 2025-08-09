import os
import json
import logging
from flask import Flask, Response, request, stream_with_context, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import google.generativeai as genai
from dotenv import load_dotenv

# --- 1. Logging and API Configuration ---
load_dotenv() # Loads the .env file for local development
logging.basicConfig(level=logging.INFO)
GEMINI_API_CONFIGURED = False
try:
    # On Render, the API key is set as an environment variable directly.
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("FATAL ERROR: GEMINI_API_KEY environment variable not set.")
    else:
        genai.configure(api_key=api_key)
        GEMINI_API_CONFIGURED = True
except Exception as e:
    print(f"FATAL ERROR: Could not configure Gemini API. Details: {e}")


# --- 2. Application Setup ---
app = Flask(__name__)
# On Render, you should set this SECRET_KEY as an environment variable for security.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-and-long-random-key-for-myth-ai-v3')

# --- 3. User and Session Management (Flask-Login) ---
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Login required.", "logged_in": False}), 401

# --- 4. Mock Database and Data Models ---
DB = {
    "users": {},
    "chats": {},
    "site_settings": {"announcement": "Welcome to the new Myth AI 2.2!"}
}

class User(UserMixin):
    def __init__(self, id, username, password_hash, role='user', plan='free'):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.role = role
        self.plan = plan
        self.daily_messages = 0
        self.last_message_date = datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def get(user_id):
        return DB['users'].get(user_id)

    @staticmethod
    def get_by_username(username):
        for user_data in DB['users'].values():
            if user_data.username == username:
                return user_data
        return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def initialize_database():
    if not User.get_by_username('nameadmin'):
        # In a real app, the admin password should also be an environment variable.
        admin_pass = os.environ.get('ADMIN_PASSWORD', 'adminadminnoob')
        admin = User(id='nameadmin', username='nameadmin', password_hash=generate_password_hash(admin_pass), role='admin', plan='pro')
        DB['users']['nameadmin'] = admin

# Initialize the database when the app starts
initialize_database()

# --- 5. HTML, CSS, and JavaScript Frontend ---
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Myth AI 2.2</title>
    <meta name="description" content="An advanced, feature-rich AI chat application prototype.">
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1136294351029434"
     crossorigin="anonymous"></script>
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
                    fontFamily: {
                        sans: ['Inter', 'sans-serif'],
                        mono: ['Fira Code', 'monospace'],
                    },
                    animation: {
                        'fade-in': 'fadeIn 0.5s ease-out forwards',
                        'scale-up': 'scaleUp 0.3s ease-out forwards',
                        'slide-in-left': 'slideInLeft 0.5s cubic-bezier(0.25, 1, 0.5, 1) forwards',
                    },
                    keyframes: {
                        fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
                        scaleUp: { '0%': { transform: 'scale(0.95)', opacity: 0 }, '100%': { transform: 'scale(1)', opacity: 1 } },
                        slideInLeft: { '0%': { transform: 'translateX(-100%)', opacity: 0 }, '100%': { transform: 'translateX(0)', opacity: 1 } },
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #111827; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #1f2937; }
        ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #6b7280; }
        .glassmorphism { background: rgba(31, 41, 55, 0.5); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.1); }
        .brand-gradient { background-image: linear-gradient(to right, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .message-wrapper { animation: fadeIn 0.4s ease-out forwards; }
        pre { position: relative; }
        .copy-code-btn { position: absolute; top: 0.5rem; right: 0.5rem; background-color: #374151; color: white; border: none; padding: 0.25rem 0.5rem; border-radius: 0.25rem; cursor: pointer; opacity: 0; transition: opacity 0.2s; font-size: 0.75rem; }
        pre:hover .copy-code-btn { opacity: 1; }
        #sidebar.hidden { transform: translateX(-100%); }
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
                    <div class="mb-4">
                        <label for="username" class="block text-sm font-medium text-gray-300 mb-1">Username</label>
                        <input type="text" id="username" name="username" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <div class="mb-6">
                        <label for="password" class="block text-sm font-medium text-gray-300 mb-1">Password</label>
                        <input type="password" id="password" name="password" class="w-full p-3 bg-gray-700/50 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" required>
                    </div>
                    <button type="submit" id="auth-submit-btn" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg transition-opacity">Login</button>
                    <p id="auth-error" class="text-red-400 text-sm text-center h-4 mt-3"></p>
                </form>
                <div class="text-center mt-6">
                    <button id="auth-toggle-btn" class="text-sm text-blue-400 hover:text-blue-300">Don't have an account? Sign Up</button>
                </div>
            </div>
             <div class="text-center mt-4">
                <button id="privacy-policy-link" class="text-xs text-gray-500 hover:text-gray-400">Privacy Policy</button>
            </div>
        </div>
    </template>

    <template id="template-app-wrapper">
        <div class="flex h-full w-full">
            <aside id="sidebar" class="bg-gray-900/70 backdrop-blur-lg w-72 flex-shrink-0 flex flex-col p-2 h-full absolute md:relative z-20 transform transition-transform duration-300 ease-in-out -translate-x-full md:translate-x-0">
                <div class="flex-shrink-0 p-2 mb-2 flex items-center gap-3">
                    <div id="app-logo-container"></div>
                    <h1 class="text-2xl font-bold brand-gradient">Myth AI 2.2</h1>
                </div>
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
                        <button id="export-chat-btn" title="Export Chat" class="p-2 rounded-lg hover:bg-gray-700/50 transition-colors"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" x2="12" y1="15" y2="3" /></svg></button>
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
                            <textarea id="user-input" placeholder="Message Myth AI..." class="w-full bg-transparent p-4 pr-16 resize-none rounded-2xl focus:outline-none focus:ring-2 focus:ring-blue-500 transition-shadow" rows="1"></textarea>
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

    <template id="template-welcome-screen">
        <div class="flex flex-col items-center justify-center h-full text-center p-4 animate-fade-in">
            <div class="w-24 h-24 mb-6" id="welcome-logo-container"></div>
            <h2 class="text-3xl md:text-4xl font-bold mb-4">Welcome to Myth AI 2.2</h2>
            <p class="text-gray-400 max-w-md">Start a new conversation or select one from the sidebar. How can I help you today?</p>
            <div class="mt-8 p-4 glassmorphism rounded-lg max-w-xl w-full">
                <label for="system-prompt-input" class="block text-sm font-medium text-gray-300 mb-2">Set a Persona (System Prompt)</label>
                <textarea id="system-prompt-input" placeholder="e.g., You are a helpful assistant that speaks like a pirate." class="w-full bg-gray-700/50 p-2 rounded-lg border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all" rows="2"></textarea>
                <button id="save-system-prompt-btn" class="mt-2 w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Set for this Chat</button>
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

    <template id="template-admin-dashboard">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex flex-wrap justify-between items-center gap-4 mb-8">
                <div class="flex items-center gap-4">
                    <div id="admin-logo-container"></div>
                    <h1 class="text-3xl font-bold brand-gradient">Admin Dashboard</h1>
                </div>
                <button id="admin-logout-btn" class="bg-red-600 hover:bg-red-500 text-white font-bold py-2 px-4 rounded-lg transition-colors">Logout</button>
            </header>

            <div class="mb-8 p-6 glassmorphism rounded-lg">
                <h2 class="text-xl font-semibold mb-4 text-white">Site Announcement</h2>
                <form id="announcement-form" class="flex flex-col sm:flex-row gap-2">
                    <input id="announcement-input" type="text" placeholder="Enter announcement text (leave empty to clear)" class="flex-grow p-2 bg-gray-700/50 rounded-lg border border-gray-600">
                    <button type="submit" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold px-4 py-2 rounded-lg">Set Banner</button>
                </form>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Total Users</h2><p id="admin-total-users" class="text-4xl font-bold text-white">0</p></div>
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Total API Calls (Today)</h2><p id="admin-total-calls" class="text-4xl font-bold text-white">0</p></div>
                <div class="p-6 glassmorphism rounded-lg"><h2 class="text-gray-400 text-lg">Pro Users</h2><p id="admin-pro-users" class="text-4xl font-bold text-white">0</p></div>
            </div>

            <div class="p-6 glassmorphism rounded-lg">
                <h2 class="text-xl font-semibold mb-4 text-white">User Management</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead class="border-b border-gray-600">
                            <tr>
                                <th class="p-2">Username</th>
                                <th class="p-2">Plan</th>
                                <th class="p-2">Usage</th>
                                <th class="p-2">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="admin-user-list"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </template>
    
    <template id="template-upgrade-page">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
            <header class="flex justify-between items-center mb-8">
                <h1 class="text-3xl font-bold brand-gradient">Upgrade Your Plan</h1>
                <button id="back-to-chat-btn" class="bg-gray-700 hover:bg-gray-600 text-white font-bold py-2 px-4 rounded-lg transition-colors">Back to Chat</button>
            </header>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-4xl mx-auto">
                <div class="p-8 glassmorphism rounded-lg border-2 border-gray-600">
                    <h2 class="text-2xl font-bold text-center text-gray-300">Free Plan</h2>
                    <p class="text-4xl font-bold text-center my-4 text-white">Free</p>
                    <ul class="space-y-2 text-gray-400">
                        <li>✓ 15 Daily Messages</li>
                        <li>✓ Standard Model Access</li>
                        <li>✓ Community Support</li>
                    </ul>
                     <button class="w-full mt-6 bg-gray-600 text-white font-bold py-3 px-4 rounded-lg cursor-not-allowed">Current Plan</button>
                </div>
                <div class="p-8 glassmorphism rounded-lg border-2 border-indigo-500">
                    <h2 class="text-2xl font-bold text-center text-indigo-400">Pro Plan</h2>
                    <p class="text-4xl font-bold text-center my-4 text-white">$9.99 <span class="text-lg font-normal text-gray-400">/ month</span></p>
                    <ul class="space-y-2 text-gray-300">
                        <li>✓ 50 Daily Messages</li>
                        <li>✓ Access to All Models</li>
                        <li>✓ Priority Support</li>
                        <li>✓ Early Access to New Features</li>
                    </ul>
                    <button id="purchase-pro-btn" class="w-full mt-6 bg-gradient-to-r from-blue-600 to-indigo-600 hover:opacity-90 text-white font-bold py-3 px-4 rounded-lg transition-opacity">Upgrade to Pro</button>
                </div>
            </div>
        </div>
    </template>

    <template id="template-privacy-policy">
        <div class="w-full h-full bg-gray-900 p-4 sm:p-6 md:p-8 overflow-y-auto">
             <header class="flex justify-between items-center mb-8">
                <h1 class="text-3xl font-bold brand-gradient">Privacy Policy</h1>
                <button id="back-to-auth-btn" class="bg-gray-700 hover:bg-gray-600 text-white font-bold py-2 px-4 rounded-lg transition-colors">Back</button>
            </header>
            <div class="max-w-4xl mx-auto glassmorphism rounded-lg p-8 prose prose-invert">
                <h2>1. Introduction</h2>
                <p>Welcome to Myth AI. This Privacy Policy explains how we collect, use, and disclose information about you when you use our service. <strong>This is a template policy and not legal advice.</strong></p>
                
                <h2>2. Information We Collect</h2>
                <p>We collect the following information:</p>
                <ul>
                    <li><strong>Account Information:</strong> When you create an account, we collect your username and a hashed version of your password.</li>
                    <li><strong>Chat History:</strong> We store your conversations to provide you with a continuous chat experience.</li>
                    <li><strong>Usage Data:</strong> We track the number of messages you send to enforce daily limits.</li>
                </ul>

                <h2>3. How We Use Your Information</h2>
                <p>We use the information we collect to:</p>
                <ul>
                    <li>Provide, maintain, and improve our services.</li>
                    <li>Manage your account and authenticate you.</li>
                    <li>Monitor and enforce our usage policies.</li>
                </ul>

                 <h2>4. Data Sharing</h2>
                <p>We do not share your personal information with third parties, except as required by law.</p>

                <h2>5. Data Security</h2>
                <p>We take reasonable measures to protect your information from loss, theft, misuse, and unauthorized access.</p>

                <h2>6. Your Rights</h2>
                <p>You have the right to access and delete your account and associated data. Please contact support for assistance.</p>
            </div>
        </div>
    </template>


<script>
/****************************************************************************
 * JAVASCRIPT FRONTEND LOGIC (MYTH AI 2.2)
 ****************************************************************************/
document.addEventListener('DOMContentLoaded', () => {
    const appState = {
        chats: {}, activeChatId: null, isAITyping: false,
        abortController: null, currentUser: null,
    };

    const DOMElements = {
        appContainer: document.getElementById('app-container'),
        modalContainer: document.getElementById('modal-container'),
        toastContainer: document.getElementById('toast-container'),
        announcementBanner: document.getElementById('announcement-banner'),
    };

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
            const data = await response.json();
            if (!response.ok) {
                if (response.status === 401) handleLogout(false);
                throw new Error(data.error || 'An unknown error occurred.');
            }
            return { success: true, ...data };
        } catch (error) {
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

    function renderAuthPage(isLogin = true) {
        const template = document.getElementById('template-auth-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('auth-logo-container');
        const title = document.getElementById('auth-title');
        const subtitle = document.getElementById('auth-subtitle');
        const submitBtn = document.getElementById('auth-submit-btn');
        const toggleBtn = document.getElementById('auth-toggle-btn');
        const form = document.getElementById('auth-form');
        const errorEl = document.getElementById('auth-error');
        if (isLogin) {
            title.textContent = 'Welcome Back';
            subtitle.textContent = 'Sign in to continue to Myth AI.';
            submitBtn.textContent = 'Login';
            toggleBtn.textContent = "Don't have an account? Sign Up";
            form.action = '/api/login';
        } else {
            title.textContent = 'Create Account';
            subtitle.textContent = 'Join Myth AI to get started.';
            submitBtn.textContent = 'Sign Up';
            toggleBtn.textContent = 'Already have an account? Login';
            form.action = '/api/signup';
        }
        toggleBtn.onclick = () => renderAuthPage(!isLogin);
        form.onsubmit = async (e) => {
            e.preventDefault();
            errorEl.textContent = '';
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            const result = await apiCall(form.action, {
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
        document.getElementById('privacy-policy-link').onclick = renderPrivacyPolicyPage;
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
        if (user.role === 'admin') {
            renderAdminDashboard();
        } else {
            renderAppUI();
        }
    }

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
    }

    function renderActiveChat() {
        const chatWindow = document.getElementById('chat-window');
        const chatTitle = document.getElementById('chat-title');
        if (!chatWindow || !chatTitle) return;
        chatWindow.innerHTML = '';
        const chat = appState.chats[appState.activeChatId];
        if (chat && chat.messages.length > 0) {
            chatTitle.textContent = chat.title;
            chat.messages.forEach(msg => addMessageToDOM(msg));
            renderCodeCopyButtons();
        } else {
            chatTitle.textContent = 'New Chat';
            renderWelcomeScreen(chat ? chat.system_prompt : '');
        }
        updateUIState();
    }

    function renderWelcomeScreen(systemPrompt = '') {
        const chatWindow = document.getElementById('chat-window');
        if (!chatWindow) return;
        const template = document.getElementById('template-welcome-screen');
        chatWindow.innerHTML = '';
        chatWindow.appendChild(template.content.cloneNode(true));
        renderLogo('welcome-logo-container');
        const systemPromptInput = document.getElementById('system-prompt-input');
        systemPromptInput.value = systemPrompt;
        document.getElementById('save-system-prompt-btn').onclick = async () => {
            if (!appState.activeChatId) {
                const chatCreated = await createNewChat(false);
                if (!chatCreated) {
                      showToast("Could not create chat to save prompt.", "error");
                      return;
                }
            }
            const prompt = systemPromptInput.value;
            const result = await apiCall('/api/chat/system_prompt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: appState.activeChatId, system_prompt: prompt }),
            });
            if (result.success) {
                appState.chats[appState.activeChatId].system_prompt = prompt;
                showToast(result.message, 'success');
            }
        };
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
                    // MOBILE FIX: Hide sidebar on selection
                    const menuToggleBtn = document.getElementById('menu-toggle-btn');
                    if (menuToggleBtn && menuToggleBtn.offsetParent !== null) { // Check if it's visible
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
        const { username, plan } = appState.currentUser;
        const planName = plan.charAt(0).toUpperCase() + plan.slice(1);
        const planColor = plan === 'pro' ? 'text-indigo-400' : 'text-gray-400';
        const avatarColor = `hsl(${username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;
        userInfoDiv.innerHTML = `
            <div class="flex items-center gap-3">
                 <div class="flex-shrink-0 w-10 h-10 rounded-full flex items-center justify-center font-bold text-white" style="background-color: ${avatarColor};">
                    ${username[0].toUpperCase()}
                </div>
                <div>
                    <div class="font-semibold">${username}</div>
                    <div class="text-xs ${planColor}">${planName} Plan</div>
                </div>
            </div>`;
        const limitDisplay = document.getElementById('message-limit-display');
        if(limitDisplay) limitDisplay.textContent = `Daily Messages: ${appState.currentUser.daily_messages} / ${appState.currentUser.message_limit}`;
    }

    function updateUIState() {
        const sendBtn = document.getElementById('send-btn');
        const stopContainer = document.getElementById('stop-generating-container');
        const chatActionButtons = ['export-chat-btn', 'rename-chat-btn', 'delete-chat-btn'];
        if (sendBtn) sendBtn.disabled = appState.isAITyping;
        if (stopContainer) stopContainer.style.display = appState.isAITyping ? 'block' : 'none';
        const chatExists = !!appState.activeChatId;
        chatActionButtons.forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.style.display = chatExists ? 'block' : 'none';
        });
    }

    async function handleSendMessage() {
        const userInput = document.getElementById('user-input');
        if (!userInput) return;
        const prompt = userInput.value.trim();
        if (!prompt || appState.isAITyping) return;

        const isFirstMessage = !appState.activeChatId || appState.chats[appState.activeChatId]?.messages.length === 0;

        if (isFirstMessage) {
            const chatWindow = document.getElementById('chat-window');
            if(chatWindow) chatWindow.innerHTML = '';
        }

        addMessageToDOM({ sender: 'user', content: prompt });
        userInput.value = '';
        userInput.style.height = 'auto';
        appState.isAITyping = true;
        appState.abortController = new AbortController();
        updateUIState();
        const aiMessage = { sender: 'model', content: '' };
        const aiContentEl = addMessageToDOM(aiMessage, true).querySelector('.message-content');

        try {
            if (!appState.activeChatId) {
                const chatCreated = await createNewChat(false);
                if (!chatCreated) {
                    throw new Error("Could not start a new chat session. Please try again.");
                }
            }

            const currentChatId = appState.activeChatId;
            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: currentChatId, prompt: prompt }),
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

            try {
                const errorJson = JSON.parse(fullResponse);
                if (errorJson.error) {
                    throw new Error(errorJson.error);
                }
            } catch (e) {
                if (!(e instanceof SyntaxError)) { throw e; }
            }

            const updatedData = await apiCall('/api/status');
            if (updatedData.success) {
                appState.currentUser = updatedData.user;
                appState.chats = updatedData.chats;
                renderChatHistoryList();
                updateUserInfo();
                renderCodeCopyButtons();
                document.getElementById('chat-title').textContent = appState.chats[currentChatId].title;
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
        const avatarColor = senderIsAI
            ? 'bg-gradient-to-br from-blue-500 to-indigo-600'
            : `background-color: hsl(${appState.currentUser.username.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0) % 360}, 50%, 60%)`;

        const aiAvatarSVG = `<svg width="20" height="20" viewBox="0 0 100 100"><path d="M35 65 L35 35 L50 50 L65 35 L65 65" stroke="white" stroke-width="8" fill="none"/></svg>`;
        const userAvatarHTML = `<div class="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white" style="${avatarColor}">${avatarChar}</div>`;
        const aiAvatarHTML = `<div class="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-white ${avatarColor}">${aiAvatarSVG}</div>`;

        wrapper.innerHTML = `
            ${senderIsAI ? aiAvatarHTML : userAvatarHTML}
            <div class="flex-1 min-w-0">
                <div class="font-bold text-gray-300">${senderIsAI ? 'Myth AI' : 'You'}</div>
                <div class="prose prose-invert max-w-none text-gray-200 message-content">
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

    function setupAppEventListeners() {
        const appContainer = document.getElementById('app-container');
        if (!appContainer) return;
        
        appContainer.onclick = (e) => {
            const target = e.target.closest('button');
            if (!target) return;
            switch (target.id) {
                case 'new-chat-btn': createNewChat(true); break;
                case 'logout-btn': handleLogout(); break;
                case 'send-btn': handleSendMessage(); break;
                case 'stop-generating-btn': appState.abortController?.abort(); break;
                case 'rename-chat-btn': handleRenameChat(); break;
                case 'delete-chat-btn': handleDeleteChat(); break;
                case 'export-chat-btn': handleExportChat(); break;
                case 'upgrade-plan-btn': renderUpgradePage(); break;
                case 'back-to-chat-btn': renderAppUI(); break;
                case 'back-to-auth-btn': renderAuthPage(); break;
                case 'purchase-pro-btn': handlePurchase(); break;
                case 'menu-toggle-btn': 
                    document.getElementById('sidebar')?.classList.toggle('-translate-x-full');
                    document.getElementById('sidebar-backdrop')?.classList.toggle('hidden');
                    break;
            }
        };

        const userInput = document.getElementById('user-input');
        if (userInput) {
            userInput.onkeydown = (e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); }
            };
            userInput.oninput = () => {
                userInput.style.height = 'auto';
                userInput.style.height = `${userInput.scrollHeight}px`;
            };
        }
        
        const backdrop = document.getElementById('sidebar-backdrop');
        if (backdrop) {
            backdrop.onclick = () => {
                document.getElementById('sidebar')?.classList.add('-translate-x-full');
                backdrop.classList.add('hidden');
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

    function handleRenameChat() {
        if (!appState.activeChatId) return;
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'w-full p-2 bg-gray-700/50 rounded-lg border border-gray-600';
        input.value = appState.chats[appState.activeChatId].title;
        openModal('Rename Chat', input, async () => {
            const newTitle = input.value.trim();
            if (newTitle) {
                const result = await apiCall('/api/chat/rename', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ chat_id: appState.activeChatId, title: newTitle }),
                });
                if (result.success) {
                    appState.chats[appState.activeChatId].title = newTitle;
                    renderActiveChat();
                    renderChatHistoryList();
                    showToast(result.message, 'success');
                }
            }
        }, 'Rename');
    }

    function handleDeleteChat() {
        if (!appState.activeChatId) return;
        openModal('Delete Chat', 'Are you sure you want to delete this chat?', async () => {
            const result = await apiCall('/api/chat/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: appState.activeChatId }),
            });
            if (result.success) {
                delete appState.chats[appState.activeChatId];
                const sortedChatIds = Object.keys(appState.chats).sort((a, b) => appState.chats[b].created_at.localeCompare(appState.chats[a].created_at));
                appState.activeChatId = sortedChatIds.length > 0 ? sortedChatIds[0] : null;
                renderActiveChat();
                renderChatHistoryList();
                showToast(result.message, 'success');
            }
        }, 'Delete');
    }

    function handleExportChat() {
        if (!appState.activeChatId) return;
        const chat = appState.chats[appState.activeChatId];
        if (!chat) return;
        let markdownContent = `# ${chat.title}\\n\\n`;
        if(chat.system_prompt) markdownContent += `**System Prompt:** ${chat.system_prompt}\\n\\n---\\n\\n`;
        chat.messages.forEach(msg => {
            const prefix = msg.sender === 'model' ? '**Myth AI**' : '**You**';
            markdownContent += `${prefix}:\\n${msg.content}\\n\\n---\\n\\n`;
        });
        const blob = new Blob([markdownContent], { type: 'text/markdown' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${chat.title.replace(/ /g, '_')}.md`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('Chat exported!', 'success');
    }
    
    function renderUpgradePage() {
        const template = document.getElementById('template-upgrade-page');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        setupAppEventListeners(); // Re-attach listeners for the new page
    }

    function renderPrivacyPolicyPage() {
        const template = document.getElementById('template-privacy-policy');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        setupAppEventListeners(); // Re-attach listeners for the new page
    }
    
    function handlePurchase() {
        const input = document.createElement('input');
        input.type = 'email';
        input.className = 'w-full p-2 bg-gray-700/50 rounded-lg border border-gray-600';
        input.placeholder = 'Enter your email to confirm';
        openModal('Confirm Purchase', input, async () => {
            if (input.value.includes('@')) { // Simple validation
                const result = await apiCall('/api/user/upgrade', { method: 'POST' });
                if (result.success) {
                    showToast(result.message, 'success');
                    appState.currentUser = result.user;
                    renderAppUI(); // Go back to chat with updated plan
                }
            } else {
                showToast('Please enter a valid email.', 'error');
            }
        }, 'Confirm Purchase');
    }

    function renderAdminDashboard() {
        const template = document.getElementById('template-admin-dashboard');
        DOMElements.appContainer.innerHTML = '';
        DOMElements.appContainer.appendChild(template.content.cloneNode(true));
        renderLogo('admin-logo-container');
        document.getElementById('admin-logout-btn').onclick = handleLogout;
        document.getElementById('announcement-form').onsubmit = handleSetAnnouncement;
        fetchAdminData();
    }

    async function fetchAdminData() {
        const data = await apiCall('/api/admin_data');
        if (!data.success) return;
        document.getElementById('admin-total-users').textContent = data.total_users;
        document.getElementById('admin-total-calls').textContent = data.total_calls_today;
        document.getElementById('admin-pro-users').textContent = data.pro_users;
        document.getElementById('announcement-input').value = data.announcement;
        const userList = document.getElementById('admin-user-list');
        userList.innerHTML = '';
        data.users.forEach(user => {
            const tr = document.createElement('tr');
            tr.className = 'border-b border-gray-700/50';
            tr.innerHTML = `
                <td class="p-2">${user.username}</td>
                <td class="p-2">${user.plan}</td>
                <td class="p-2">${user.daily_messages} / ${user.message_limit}</td>
                <td class="p-2 flex gap-2">
                    <button data-userid="${user.id}" class="toggle-plan-btn text-xs px-2 py-1 rounded ${user.plan === 'pro' ? 'bg-yellow-600' : 'bg-blue-600'}">
                        ${user.plan === 'pro' ? 'Make Free' : 'Make Pro'}
                    </button>
                    <button data-userid="${user.id}" class="delete-user-btn text-xs px-2 py-1 rounded bg-red-600">Delete</button>
                </td>`;
            userList.appendChild(tr);
        });
        userList.querySelectorAll('.toggle-plan-btn').forEach(btn => btn.onclick = handleAdminTogglePlan);
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

    async function handleAdminTogglePlan(e) {
        const userId = e.target.dataset.userid;
        const result = await apiCall('/api/admin/toggle_plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId }),
        });
        if (result.success) {
            showToast(result.message, 'success');
            fetchAdminData();
        }
    }

    function handleAdminDeleteUser(e) {
        const userId = e.target.dataset.userid;
        openModal('Delete User', `Are you sure you want to permanently delete user ${userId}?`, async () => {
            const result = await apiCall('/api/admin/delete_user', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId }),
            });
            if (result.success) {
                showToast(result.message, 'success');
                fetchAdminData();
            }
        }, 'Delete User');
    }

    checkLoginStatus();
});
</script>
</body>
</html>
"""

# --- 6. Backend Logic (Flask Routes) ---
PLAN_CONFIG = {
    "free": {"message_limit": 15, "models": ["gemini-1.5-flash-latest"]},
    "pro": {"message_limit": 50, "models": ["gemini-1.5-flash-latest", "gemini-pro"]}
}

def check_and_reset_daily_limit(user):
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user.last_message_date != today_str:
        user.last_message_date = today_str
        user.daily_messages = 0

def get_user_data_for_frontend(user):
    if not user: return {}
    check_and_reset_daily_limit(user)
    plan_details = PLAN_CONFIG.get(user.plan, PLAN_CONFIG['free'])
    return {
        "id": user.id, "username": user.username, "role": user.role, "plan": user.plan,
        "daily_messages": user.daily_messages, "message_limit": plan_details["message_limit"]
    }

def get_all_user_chats(user_id):
    user_chats = {}
    for chat_id, chat_data in DB['chats'].items():
        if chat_data.get('user_id') == user_id:
            user_chats[chat_id] = chat_data
    return user_chats

@app.route('/')
def index():
    return Response(HTML_CONTENT, mimetype='text/html')

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    username, password = data.get('username'), data.get('password')
    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400
    if User.get_by_username(username):
        return jsonify({"error": "Username already exists."}), 409
    new_user = User(id=username, username=username, password_hash=generate_password_hash(password))
    DB['users'][new_user.id] = new_user
    login_user(new_user, remember=True)
    return jsonify({
        "success": True, "user": get_user_data_for_frontend(new_user),
        "chats": {}, "settings": DB['site_settings']
    })

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username, password = data.get('username'), data.get('password')
    user = User.get_by_username(username)
    if user and check_password_hash(user.password_hash, password):
        login_user(user, remember=True)
        return jsonify({
            "success": True, "user": get_user_data_for_frontend(user),
            "chats": get_all_user_chats(user.id), "settings": DB['site_settings']
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
            "chats": get_all_user_chats(current_user.id), "settings": DB['site_settings']
        })
    return jsonify({"logged_in": False})

@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api():
    if not GEMINI_API_CONFIGURED:
        return jsonify({"error": "Gemini API is not configured on the server."}), 503

    data = request.get_json()
    chat_id, prompt = data.get('chat_id'), data.get('prompt')
    if not all([chat_id, prompt]):
        return jsonify({"error": "Missing chat_id or prompt."}), 400

    chat = DB['chats'].get(chat_id)
    if not chat or chat.get('user_id') != current_user.id:
        return jsonify({"error": "Chat not found or access denied."}), 404

    check_and_reset_daily_limit(current_user)
    plan_details = PLAN_CONFIG[current_user.plan]
    if current_user.daily_messages >= plan_details["message_limit"]:
        return jsonify({"error": f"Daily message limit of {plan_details['message_limit']} reached."}), 429

    history = []
    system_instruction = chat.get('system_prompt')

    for msg in chat['messages']:
        role = 'model' if msg['sender'] == 'model' else 'user'
        history.append({"role": role, "parts": [{"text": msg['content']}]})

    try:
        model = genai.GenerativeModel(
            'gemini-1.5-flash-latest',
            system_instruction=system_instruction if system_instruction else None
        )
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
                logging.error(f"Error during Gemini stream for chat {chat_id}: {e}")
                yield json.dumps({"error": f"An error occurred with the AI model: {str(e)}"})
                return

            try:
                if not full_response_text.strip():
                    logging.info(f"Received an empty response for chat {chat_id}.")
                    return

                chat['messages'].append({'sender': 'user', 'content': prompt})
                chat['messages'].append({'sender': 'model', 'content': full_response_text})
                current_user.daily_messages += 1

                if len(chat['messages']) == 2:
                    try:
                        title_prompt = f"Summarize the following conversation with a short, descriptive title (4 words max).\\n\\nUser: \"{prompt}\"\\nAssistant: \"{full_response_text}\""
                        title_response = genai.GenerativeModel('gemini-1.5-flash-latest').generate_content(title_prompt)
                        title_text = title_response.text.strip().replace('"', '')
                        chat['title'] = title_text if title_text else (prompt[:40] + '...')
                    except Exception as title_e:
                        logging.error(f"Could not generate title for chat {chat_id}: {title_e}")
                        chat['title'] = prompt[:40] + '...' if len(prompt) > 40 else prompt
            except Exception as db_e:
                logging.error(f"FATAL: Could not save chat history for {chat_id}: {db_e}")

        return Response(stream_with_context(generate_chunks()), mimetype='text/plain')

    except Exception as e:
        logging.error(f"Fatal error in /api/chat setup for chat {chat_id}: {str(e)}")
        return jsonify({"error": f"An internal server error occurred: {str(e)}"}), 500

@app.route('/api/chat/new', methods=['POST'])
@login_required
def new_chat():
    chat_id = f"chat_{current_user.id}_{datetime.now().timestamp()}"
    new_chat_data = {
        "id": chat_id, "user_id": current_user.id, "title": "New Chat",
        "messages": [], "system_prompt": "", "created_at": datetime.now().isoformat()
    }
    DB['chats'][chat_id] = new_chat_data
    return jsonify({"success": True, "chat": new_chat_data})

@app.route('/api/chat/rename', methods=['POST'])
@login_required
def rename_chat():
    data = request.get_json()
    chat_id, new_title = data.get('chat_id'), data.get('title')
    chat = DB['chats'].get(chat_id)
    if chat and chat['user_id'] == current_user.id:
        chat['title'] = new_title
        return jsonify({"success": True, "message": "Chat renamed."})
    return jsonify({"error": "Chat not found or access denied."}), 404

@app.route('/api/chat/delete', methods=['POST'])
@login_required
def delete_chat():
    chat_id = request.json.get('chat_id')
    chat = DB['chats'].get(chat_id)
    if chat and chat['user_id'] == current_user.id:
        del DB['chats'][chat_id]
        return jsonify({"success": True, "message": "Chat deleted."})
    return jsonify({"error": "Chat not found or access denied."}), 404

@app.route('/api/chat/system_prompt', methods=['POST'])
@login_required
def set_system_prompt():
    data = request.get_json()
    chat_id, system_prompt = data.get('chat_id'), data.get('system_prompt')
    chat = DB['chats'].get(chat_id)
    if chat and chat['user_id'] == current_user.id:
        chat['system_prompt'] = system_prompt
        return jsonify({"success": True, "message": "System prompt updated."})
    return jsonify({"error": "Chat not found or access denied."}), 404
    
@app.route('/api/user/upgrade', methods=['POST'])
@login_required
def upgrade_user_plan():
    if current_user.plan == 'pro':
        return jsonify({"error": "User is already on the Pro plan."}), 400
    
    current_user.plan = 'pro'
    return jsonify({
        "success": True, 
        "message": "Congratulations! You've upgraded to the Pro Plan.",
        "user": get_user_data_for_frontend(current_user)
    })

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return decorated_function

@app.route('/api/admin_data')
@admin_required
def admin_data():
    all_users_data, total_calls_today, pro_users = [], 0, 0
    for user in DB["users"].values():
        if user.role == "user":
            check_and_reset_daily_limit(user)
            plan_details = PLAN_CONFIG.get(user.plan, PLAN_CONFIG['free'])
            all_users_data.append({
                "id": user.id, "username": user.username, "plan": user.plan,
                "daily_messages": user.daily_messages, "message_limit": plan_details['message_limit']
            })
            total_calls_today += user.daily_messages
            if user.plan == 'pro': pro_users += 1
    return jsonify({
        "total_users": len(all_users_data), "total_calls_today": total_calls_today,
        "pro_users": pro_users, "users": sorted(all_users_data, key=lambda x: x['username']),
        "announcement": DB['site_settings']['announcement']
    })

@app.route('/api/admin/toggle_plan', methods=['POST'])
@admin_required
def admin_toggle_plan():
    user = User.get(request.json.get('user_id'))
    if not user: return jsonify({"error": "User not found."}), 404
    user.plan = 'free' if user.plan == 'pro' else 'pro'
    return jsonify({"success": True, "message": f"{user.username}'s plan set to {user.plan}."})

@app.route('/api/admin/delete_user', methods=['POST'])
@admin_required
def admin_delete_user():
    user_id = request.json.get('user_id')
    if user_id == 'nameadmin': return jsonify({"error": "Cannot delete the primary admin account."}), 400
    if user_id in DB['users']:
        del DB['users'][user_id]
        chats_to_delete = [cid for cid, c in DB['chats'].items() if c['user_id'] == user_id]
        for cid in chats_to_delete: del DB['chats'][cid]
        return jsonify({"success": True, "message": f"User {user_id} and their chats deleted."})
    return jsonify({"error": "User not found."}), 404

@app.route('/api/admin/announcement', methods=['POST'])
@admin_required
def set_announcement():
    DB['site_settings']['announcement'] = request.json.get('text', '')
    return jsonify({"success": True, "message": "Announcement updated."})

# This part is for local execution only. Gunicorn on Render will not run this.
if __name__ == '__main__':
    # The host must be '0.0.0.0' to be accessible within Render's container
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=Fal



