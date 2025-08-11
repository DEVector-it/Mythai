# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess'
    SECURITY_PASSWORD_SALT = SECRET_KEY
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    
    # Session Cookie Settings
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Rate Limiting
    RATELIMIT_STORAGE_URI = "memory://"
    
    # API Keys
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    
    # Stripe Keys
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
    STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
    STRIPE_STUDENT_PRICE_ID = os.environ.get('STRIPE_STUDENT_PRICE_ID')
    STRIPE_STUDENT_PRO_PRICE_ID = os.environ.get('STRIPE_STUDENT_PRO_PRICE_ID')
    
    # App Keys & Domain
    SECRET_REGISTRATION_KEY = os.environ.get('SECRET_REGISTRATION_KEY')
    SECRET_TEACHER_KEY = os.environ.get('SECRET_TEACHER_KEY')
    YOUR_DOMAIN = os.environ.get('YOUR_DOMAIN', 'http://127.0.0.1:5000')

    # Google OAuth
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
    
    # Mail Server
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', '1', 't']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_SENDER = os.environ.get('MAIL_SENDER')

class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///dev.db'

class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    FLASK_ENV = 'production'
    SESSION_COOKIE_SECURE = True # Enforce HTTPS for session cookies
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    
    # Use Redis for rate limiting in production for better scalability
    RATELIMIT_STORAGE_URI = os.environ.get('REDIS_URL', 'memory://')

# Dictionary to access config classes by name
config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig
}
