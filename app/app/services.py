# app/services.py
import os
import logging
import smtplib
import base64
from io import BytesIO
from email.mime.text import MIMEText

import google.generativeai as genai
import stripe
from PIL import Image
from flask import current_app, url_for
from itsdangerous import URLSafeTimedSerializer

def get_gemini_model(plan):
    """Gets the appropriate Gemini model based on user plan."""
    if not current_app.config.get('GEMINI_API_KEY'):
        logging.error("GEMINI_API_KEY not configured.")
        return None
        
    genai.configure(api_key=current_app.config['GEMINI_API_KEY'])
    model_name = "gemini-1.5-pro-latest" if plan == 'student_pro' else "gemini-1.5-flash-latest"
    return genai.GenerativeModel(model_name)

def get_gemini_title_model():
    """Gets the flash model specifically for generating titles."""
    if not current_app.config.get('GEMINI_API_KEY'):
        return None
    genai.configure(api_key=current_app.config['GEMINI_API_KEY'])
    return genai.GenerativeModel('gemini-1.5-flash-latest')

def process_image_for_gemini(uploaded_file):
    """Resizes, converts, and base64-encodes an image for the Gemini API."""
    try:
        img = Image.open(uploaded_file.stream)
        img.thumbnail((512, 512)) # Resize to a reasonable size
        buffered = BytesIO()
        if img.mode in ("RGBA", "P"): # Convert formats with transparency
            img = img.convert("RGB")
        img.save(buffered, format="JPEG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
        return {"inline_data": {"mime_type": "image/jpeg", "data": img_base64}}
    except Exception as e:
        logging.error(f"Image processing error: {e}")
        return None

def send_email(recipient, subject, html_body):
    """Sends an email using configured SMTP settings."""
    config = current_app.config
    if not all([config['MAIL_SERVER'], config['MAIL_USERNAME'], config['MAIL_PASSWORD']]):
        logging.error("Mail server is not configured. Cannot send email.")
        return False
        
    msg = MIMEText(html_body, 'html')
    msg['Subject'] = subject
    msg['From'] = config['MAIL_SENDER']
    msg['To'] = recipient

    try:
        with smtplib.SMTP(config['MAIL_SERVER'], config['MAIL_PORT']) as server:
            if config['MAIL_USE_TLS']:
                server.starttls()
            server.login(config['MAIL_USERNAME'], config['MAIL_PASSWORD'])
            server.send_message(msg)
        logging.info(f"Email sent to {recipient} with subject '{subject}'")
        return True
    except Exception as e:
        logging.error(f"Failed to send email to {recipient}: {e}")
        return False

def generate_password_reset_token(email):
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset-salt')

def verify_password_reset_token(token, max_age=3600):
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=max_age)
        return email
    except Exception:
        return None

def create_stripe_checkout_session(plan_id, user_id):
    """Creates a Stripe Checkout session for a given plan."""
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    
    price_map = {
        "student": current_app.config['STRIPE_STUDENT_PRICE_ID'],
        "student_pro": current_app.config['STRIPE_STUDENT_PRO_PRICE_ID']
    }
    
    price_id = price_map.get(plan_id)
    if not price_id:
        raise ValueError("Invalid plan ID specified.")
        
    try:
        checkout_session = stripe.checkout.Session.create(
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=current_app.config['YOUR_DOMAIN'] + '/?payment=success',
            cancel_url=current_app.config['YOUR_DOMAIN'] + '/?payment=cancel',
            client_reference_id=user_id
        )
        return checkout_session
    except Exception as e:
        logging.error(f"Stripe session creation error: {e}")
        raise
