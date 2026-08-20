from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    deepl_api_key = db.Column(db.String(256))
    webflow_token = db.Column(db.String(256))
    site_id = db.Column(db.String(100))
    webflow_webhook_secret = db.Column(db.String(256))
    admin_email = db.Column(db.String(120), default="emanueel031@gmail.com")
    smtp_email = db.Column(db.String(120), default="supportitgv@gmail.com")
    smtp_password = db.Column(db.String(256))

class TranslationRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(100), index=True) 
    item_type = db.Column(db.String(50)) 
    translation_count = db.Column(db.Integer, default=0)
    last_translated = db.Column(db.DateTime, default=datetime.utcnow)
    content_hash = db.Column(db.String(256))

class AutoRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.String(100)) 
    target_type = db.Column(db.String(50)) 
    trigger_type = db.Column(db.String(50)) 
    frequency_days = db.Column(db.Integer, default=3)
    modified_within_days = db.Column(db.Integer, default=5)
    is_active = db.Column(db.Boolean, default=True)
    last_run = db.Column(db.DateTime, default=datetime.utcnow)
    target_name = db.Column(db.String(150))
