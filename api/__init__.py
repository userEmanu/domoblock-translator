import os
from flask import Flask
from api.models import db
from api.routes import main

def create_app():
    app = Flask(__name__, template_folder='templates')
    
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default_local_secret_domoblock2026')
    
    db_url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL') or 'sqlite:///local.db'
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()
        from api.models import User
        if not User.query.filter_by(username='admin').first():
            # Crear administrador inicial
            user = User(username='admin')
            user.set_password('AdminDomoblock2026*') 
            db.session.add(user)
            db.session.commit()

    return app
