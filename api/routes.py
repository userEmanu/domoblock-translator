import os
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, jsonify
from datetime import datetime, timedelta
import requests
from api.models import db, User, Settings, AutoRule
from api.services import TranslatorService, send_login_alert

main = Blueprint('main', __name__)

RECAPTCHA_SECRET = '6Lcomo4tAAAAABXYSj-xbZdUSxE2CHfP_BtNeUGa'

def login_required(f):
    def wrapper(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('main.login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def get_translator():
    config = Settings.query.first()
    if config and config.deepl_api_key and config.webflow_token and config.site_id:
        return TranslatorService(config.webflow_token, config.deepl_api_key), config
    return None, None

@main.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        recaptcha_response = request.form.get('g-recaptcha-response')

        # Validación reCAPTCHA v3 (Invisible e Impenetrable)
        verify_url = 'https://www.google.com/recaptcha/api/siteverify'
        r_result = requests.post(verify_url, data={'secret': RECAPTCHA_SECRET, 'response': recaptcha_response}).json()

        # En reCAPTCHA v3 evaluamos que sea exitoso y que el score sea alto (>= 0.5 es humano)
        if not r_result.get('success') or r_result.get('score', 0) < 0.5:
            flash("Verificación reCAPTCHA fallida o comportamiento sospechoso detectado.", "danger")
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['logged_in'] = True
            config = Settings.query.first()
            if config and config.admin_email:
                send_login_alert(config.admin_email)
            return redirect(url_for('main.dashboard'))
        else:
            flash("Credenciales incorrectas", "danger")
            
    return render_template('login.html')

@main.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('main.login'))

@main.route('/')
@main.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    config = Settings.query.first()
    if not config:
        config = Settings(admin_email="emanueel031@gmail.com")
        db.session.add(config)
        db.session.commit()

    if request.method == 'POST':
        config.deepl_api_key = request.form.get('deepl_key')
        config.webflow_token = request.form.get('webflow_token')
        config.site_id = request.form.get('site_id')
        db.session.commit()
        flash("Configuración guardada exitosamente.", "success")

    usage = "API no configurada"
    translator, _ = get_translator()
    if translator:
        usage = translator.get_deepl_usage()

    return render_template('dashboard.html', config=config, usage=usage)

@main.route('/manual')
@login_required
def manual():
    translator, config = get_translator()
    if not translator:
        flash("Configura las APIs en el Dashboard primero.", "warning")
        return redirect(url_for('main.dashboard'))
    
    pages = translator.get_pages(config.site_id)
    collections = translator.get_collections(config.site_id)
    return render_template('manual.html', pages=pages, collections=collections)

@main.route('/auto', methods=['GET', 'POST'])
@login_required
def auto():
    translator, config = get_translator()
    if not translator:
        flash("Configura las APIs en el Dashboard primero.", "warning")
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        target_id = request.form.get('target_id')
        target_type = request.form.get('target_type')
        trigger_type = request.form.get('trigger_type')
        frequency_days = int(request.form.get('frequency_days', 3))
        modified_within_days = int(request.form.get('modified_within_days', 5))
        target_name = request.form.get('target_name', 'Recurso sin nombre')
        
        exists = AutoRule.query.filter_by(target_id=target_id).first()
        if exists: db.session.delete(exists)

        new_rule = AutoRule(
            target_id=target_id, target_type=target_type, trigger_type=trigger_type, 
            frequency_days=frequency_days, modified_within_days=modified_within_days,
            target_name=target_name, is_active=True
        )
        db.session.add(new_rule)
        db.session.commit()
        flash("Regla de automatización agregada/actualizada con éxito.", "success")

    rules = AutoRule.query.all()
    pages = translator.get_pages(config.site_id)
    collections = translator.get_collections(config.site_id)
    
    return render_template('auto.html', rules=rules, pages=pages, collections=collections)

@main.route('/auto/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_auto(id):
    rule = AutoRule.query.get_or_404(id)
    rule.is_active = not rule.is_active
    db.session.commit()
    flash(f"Regla {'Activada' if rule.is_active else 'Desactivada'} correctamente.", "success")
    return redirect(url_for('main.auto'))

@main.route('/auto/delete/<int:id>', methods=['POST'])
@login_required
def delete_auto(id):
    rule = AutoRule.query.get_or_404(id)
    db.session.delete(rule)
    db.session.commit()
    flash("Regla eliminada.", "success")
    return redirect(url_for('main.auto'))

# ==========================================
# ENDPOINTS AUTOMÁTICOS (CRON Y WEBHOOKS)
# ==========================================

@main.route('/api/cron/translate', methods=['GET', 'POST'])
def cron_translate():
    auth_header = request.headers.get('Authorization')
    expected_secret = f"Bearer {os.environ.get('CRON_SECRET', 'dev_secret')}"
    if auth_header != expected_secret and os.environ.get('FLASK_ENV') != 'development':
        return jsonify({'error': 'No autorizado'}), 401

    translator, config = get_translator()
    if not translator: return jsonify({"status": "no config"}), 200

    es_loc, en_loc = translator.get_locales(config.site_id)
    if not es_loc or not en_loc: return jsonify({"status": "locales error"}), 200

    rules = AutoRule.query.filter_by(is_active=True, trigger_type='cron').all()
    now = datetime.utcnow()
    translated_count = 0

    for rule in rules:
        if (now - rule.last_run).days < rule.frequency_days:
            continue
            
        if rule.target_type == 'collection':
            items = translator.get_items(rule.target_id, es_loc['cmsLocaleId'])
            for item in items:
                date_str = item.get('updatedOn', '')[:19]
                if date_str:
                    updated_obj = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                    if (now - updated_obj).days <= rule.modified_within_days:
                        if translator.process_cms_item(rule.target_id, item, en_loc['cmsLocaleId']):
                            translated_count += 1

        elif rule.target_type == 'page':
            pages = translator.get_pages(config.site_id)
            target_page = next((p for p in pages if p['id'] == rule.target_id), None)
            if target_page:
                date_str = target_page.get('lastUpdated', '')[:19]
                if date_str:
                    updated_obj = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                    if (now - updated_obj).days <= rule.modified_within_days:
                        if translator.process_page_dom(rule.target_id, es_loc['id'], en_loc['id']):
                            translated_count += 1
                            
        rule.last_run = now
        db.session.commit()

    return jsonify({"status": "Cron finalizado con éxito", "items_traducidos": translated_count})

@main.route('/api/webhook/webflow', methods=['POST'])
def webflow_webhook():
    data = request.json
    if not data: return jsonify({"status": "No data"}), 400

    translator, config = get_translator()
    if not translator: return jsonify({"status": "No config"}), 200

    es_loc, en_loc = translator.get_locales(config.site_id)
    if not es_loc: return jsonify({"status": "locales error"}), 200

    item_id = data.get('_id')
    collection_id = data.get('_cid')
    
    if collection_id and item_id:
        rule = AutoRule.query.filter_by(target_id=collection_id, target_type='collection', trigger_type='webhook', is_active=True).first()
        if rule:
            full_item = translator.get_single_item(collection_id, item_id, es_loc['cmsLocaleId'])
            if full_item:
                translator.process_cms_item(collection_id, full_item, en_loc['cmsLocaleId'])
                return jsonify({"status": "CMS Item procesado vía Webhook"})

    elif data.get('siteId') == config.site_id:
        page_id = data.get('pageId')
        if page_id and AutoRule.query.filter_by(target_id=page_id, target_type='page').first():
            translator.process_page_dom(page_id, es_loc['id'], en_loc['id'])
            return jsonify({"status": "Page processed"})
        
        page_rules = AutoRule.query.filter_by(target_type='page', trigger_type='webhook', is_active=True).all()
        processed = 0
        for rule in page_rules:
            if translator.process_page_dom(rule.target_id, es_loc['id'], en_loc['id']):
                processed += 1
                
        return jsonify({"status": f"Site Publish detectado. {processed} páginas procesadas vía Webhook."})

    return jsonify({"status": "Ignorado - No hay reglas activas para este recurso"})
