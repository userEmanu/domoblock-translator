import os
import tempfile
from flask import Flask
from api.models import db
from api.routes import main
from sqlalchemy import inspect, text

def create_app():
    app = Flask(__name__, template_folder='templates')
    
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default_local_secret_domoblock2026')
    
    tmp_db_path = os.path.join(tempfile.gettempdir(), 'local.db')
    fallback_uri = f'sqlite:///{tmp_db_path}'
    
    db_url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL') or fallback_uri
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()
        
        # --- AUTO MIGRACIÓN DE ESQUEMA ---
        # Garantiza que cualquier columna nueva se añada sin generar Errores 500
        try:
            inspector = inspect(db.engine)
            if inspector.has_table('settings'):
                columns = [col['name'] for col in inspector.get_columns('settings')]
                with db.engine.begin() as conn:
                    if 'smtp_email' not in columns:
                        conn.execute(text('ALTER TABLE settings ADD COLUMN smtp_email VARCHAR(120)'))
                    if 'smtp_password' not in columns:
                        conn.execute(text('ALTER TABLE settings ADD COLUMN smtp_password VARCHAR(256)'))
                    if 'admin_email' not in columns:
                        conn.execute(text('ALTER TABLE settings ADD COLUMN admin_email VARCHAR(120)'))
            
            if inspector.has_table('auto_rule'):
                columns = [col['name'] for col in inspector.get_columns('auto_rule')]
                with db.engine.begin() as conn:
                    if 'webhook_secret' not in columns:
                        conn.execute(text('ALTER TABLE auto_rule ADD COLUMN webhook_secret VARCHAR(256)'))
        except Exception as e:
            print(f"Error en auto-migración de base de datos: {e}")

        # Creación de usuario administrador primario
        from api.models import User
        try:
            if not User.query.filter_by(username='admin').first():
                user = User(username='admin')
                user.set_password('AdminDomoblock2026*') 
                db.session.add(user)
                db.session.commit()
        except Exception:
            pass

    return app
