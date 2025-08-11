# app/__init__.py
import os
import logging
from flask import Flask, g
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import config_by_name

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = 'auth.login' # Redirect to the login page
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)

# Content Security Policy
csp = {
    'default-src': ["'self'"],
    'script-src': [
        "'self'", 
        "https://js.stripe.com", 
        "https://cdn.tailwindcss.com", 
        "https://cdnjs.cloudflare.com", 
        "https://accounts.google.com/gsi/client"
    ],
    'style-src': ["'self'", "https://cdn.tailwindcss.com", "https://fonts.googleapis.com", "'unsafe-inline'", "https://accounts.google.com/gsi/style"],
    'font-src': ["'self'", "https://fonts.gstatic.com"],
    'img-src': ["'self'", "data:", "https://*.stripe.com", "https://lh3.googleusercontent.com"],
    'connect-src': ["'self'", "https://api.stripe.com", "https://accounts.google.com/gsi/"],
    'frame-src': ["https://js.stripe.com", "https://accounts.google.com/gsi/"]
}

def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    
    # Use Talisman for security headers
    Talisman(app, content_security_policy=csp)
    
    # Set up logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    with app.app_context():
        from . import models

        @app.before_request
        def before_request_handler():
            g.user = current_user

        # Register Blueprints
        from .routes.core import core_bp
        from .routes.auth import auth_bp
        from .routes.chat import chat_bp
        from .routes.admin import admin_bp
        from .routes.teacher import teacher_bp
        
        app.register_blueprint(core_bp)
        app.register_blueprint(auth_bp, url_prefix='/api/auth')
        app.register_blueprint(chat_bp, url_prefix='/api/chat')
        app.register_blueprint(admin_bp, url_prefix='/api/admin')
        app.register_blueprint(teacher_bp, url_prefix='/api/teacher')

        # Create database tables if they don't exist
        db.create_all()

        # Initialize default admin user
        from .models import User
        if not User.query.filter_by(username='admin').first():
            from werkzeug.security import generate_password_hash
            admin_pass = os.environ.get('ADMIN_PASSWORD', 'default_admin_pass')
            admin_email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
            admin = User(
                username='admin', 
                email=admin_email, 
                password_hash=generate_password_hash(admin_pass), 
                role='admin', 
                plan='student_pro', 
                account_type='admin',
                is_email_verified=True
            )
            db.session.add(admin)
            db.session.commit()
            logging.info("Created default admin user.")

        return app
