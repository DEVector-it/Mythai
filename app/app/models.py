# app/models.py
import uuid
from datetime import datetime, date, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from . import db, login_manager

# Association table for classrooms and students
classroom_student_association = db.Table('classroom_student',
    db.Column('user_id', db.String(36), db.ForeignKey('user.id'), primary_key=True),
    db.Column('classroom_id', db.String(36), db.ForeignKey('classroom.id'), primary_key=True)
)

class User(UserMixin, db.Model):
    __tablename__ = 'user'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    google_id = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)
    
    role = db.Column(db.String(20), default='user', nullable=False) # 'user', 'admin'
    account_type = db.Column(db.String(20), default='student', nullable=False) # 'student', 'teacher', 'admin'
    plan = db.Column(db.String(20), default='student', nullable=False) # 'student', 'student_pro'
    
    daily_messages = db.Column(db.Integer, default=0)
    last_message_date = db.Column(db.Date, default=date.today)
    streak = db.Column(db.Integer, default=0)
    last_streak_date = db.Column(db.Date, default=date.today)
    
    message_limit_override = db.Column(db.Integer, nullable=True)
    
    is_email_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    chats = db.relationship('Chat', backref='author', lazy='dynamic', cascade="all, delete-orphan")
    api_keys = db.relationship('APIKey', backref='user', lazy='dynamic', cascade="all, delete-orphan")
    audit_logs = db.relationship('AuditLog', backref='user', lazy='dynamic', cascade="all, delete-orphan")
    
    # For teachers
    owned_classroom = db.relationship('Classroom', backref='teacher', uselist=False, foreign_keys='Classroom.teacher_id')
    
    # For students
    classrooms = db.relationship('Classroom', secondary=classroom_student_association, back_populates='students')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
        
    def check_and_update_limits(self):
        today = date.today()
        if self.last_message_date != today:
            self.last_message_date = today
            self.daily_messages = 0
            self.message_limit_override = None # Reset override daily
        
        if self.last_streak_date < (today - timedelta(days=1)):
            self.streak = 0
        
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

class Classroom(db.Model):
    __tablename__ = 'classroom'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    teacher_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    students = db.relationship('User', secondary=classroom_student_association, back_populates='classrooms')

class Chat(db.Model):
    __tablename__ = 'chat'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(100), default="New Chat")
    messages = db.Column(db.JSON, default=list)
    is_public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, index=True, default=datetime.utcnow)

class APIKey(db.Model):
    __tablename__ = 'api_key'
    
    id = db.Column(db.Integer, primary_key=True)
    key_prefix = db.Column(db.String(8), unique=True, nullable=False)
    hashed_key = db.Column(db.String(256), nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=True)
    event_type = db.Column(db.String(50), nullable=False)
    details = db.Column(db.String(255), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

class SiteSettings(db.Model):
    __tablename__ = 'site_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.JSON, nullable=False)
