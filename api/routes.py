import os
import hmac
import hashlib
import time
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, jsonify
from datetime import datetime, timedelta
import requests
from api.models import db, User, Settings, AutoRule
from api.services import TranslatorService, send_login_alert, send_webhook_log

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
    try:
        config = Settings.query.first()
        if config and config.deepl_api_key and config.webflow_token and config.site_id:
            return TranslatorService(config.webflow_token, config.deepl_api_key), config
    except Exception as e:
        print(f"Error accediendo a Settings: {e}")
    return None, None

# ==========================================
# LOGIN Y DASHBOARD
# ==========================================

@main.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        recaptcha_response = request.form.get('g-recaptcha-response')
        verify_url = 'https://www.google.com/recaptcha/api/siteverify'
        r_result = requests.post(verify_url, data={'secret': RECAPTCHA_SECRET, 'response': recaptcha_response}).json()
        if not r_result.get('success') or r_result.get('score', 0) < 0.5:
            flash("Verificación reCAPTCHA fallida o comportamiento de Bot detectado.", "danger")
            return render_template('login.html')
        try:
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                session['logged_in'] = True
                config = Settings.query.first()
                if config and config.admin_email and config.smtp_email and config.smtp_password:
                    send_login_alert(config.admin_email, config.smtp_email, config.smtp_password)
                return redirect(url_for('main.dashboard'))
            else:
                flash("Credenciales incorrectas", "danger")
        except Exception as e:
            flash(f"Error de base de datos durante el login: {e}", "danger")
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
        config = Settings(admin_email="emanueel031@gmail.com", smtp_email="supportitgv@gmail.com")
        db.session.add(config)
        db.session.commit()
    if request.method == 'POST':
        config.deepl_api_key = request.form.get('deepl_key')
        config.webflow_token = request.form.get('webflow_token')
        config.site_id = request.form.get('site_id')
        config.admin_email = request.form.get('admin_email')
        config.smtp_email = request.form.get('smtp_email')
        config.smtp_password = request.form.get('smtp_password')
        db.session.commit()
        flash("Configuración guardada exitosamente.", "success")
    usage = "API no configurada"
    translator, _ = get_translator()
    if translator:
        usage = translator.get_deepl_usage()
    utc_now = datetime.utcnow()
    colombia_time = utc_now - timedelta(hours=5)
    return render_template('dashboard.html', config=config, usage=usage, current_time=colombia_time.strftime('%Y-%m-%d %I:%M %p'))

# ==========================================
# RUTAS DE TRADUCCIÓN MANUAL
# ==========================================

@main.route('/manual')
@login_required
def manual():
    translator, config = get_translator()
    if not translator:
        flash("Configure las APIs en el Dashboard primero.", "warning")
        return redirect(url_for('main.dashboard'))
    pages = translator.get_pages(config.site_id)
    collections = translator.get_collections(config.site_id)
    components = translator.get_components(config.site_id)
    return render_template('manual.html', pages=pages, collections=collections, components=components)

@main.route('/api/items/<collection_id>')
@login_required
def get_collection_items(collection_id):
    translator, config = get_translator()
    if not translator:
        return jsonify([])
    es_loc, _ = translator.get_locales(config.site_id)
    items = translator.get_items(collection_id, es_loc['cmsLocaleId'])
    return jsonify([{'id': i['id'], 'name': i.get('fieldData', {}).get('name', 'Sin Nombre')} for i in items])

@main.route('/manual/translate', methods=['POST'])
@login_required
def manual_translate():
    translator, config = get_translator()
    if not translator:
        return redirect(url_for('main.manual'))
    es_loc, en_loc = translator.get_locales(config.site_id)
    target_type = request.form.get('target_type')
    target_id = request.form.get('target_id')
    item_id = request.form.get('item_id')
    processed = 0

    if target_type == 'page':
        es_id, en_id = es_loc['id'], en_loc['id']
        if target_id == 'all':
            for page in translator.get_pages(config.site_id):
                if translator.process_page_dom(page['id'], es_id, en_id, force=True):
                    processed += 1
        else:
            if translator.process_page_dom(target_id, es_id, en_id, force=True):
                processed += 1

    elif target_type == 'collection':
        es_id, en_id = es_loc['cmsLocaleId'], en_loc['cmsLocaleId']
        if target_id == 'all':
            for col in translator.get_collections(config.site_id):
                for item in translator.get_items(col['id'], es_id):
                    if translator.process_cms_item(col['id'], item, en_id, force=True):
                        processed += 1
        else:
            if item_id == 'all':
                for item in translator.get_items(target_id, es_id):
                    if translator.process_cms_item(target_id, item, en_id, force=True):
                        processed += 1
            else:
                item = translator.get_single_item(target_id, item_id, es_id)
                if item and translator.process_cms_item(target_id, item, en_id, force=True):
                    processed += 1

    elif target_type == 'component':
        es_id, en_id = es_loc['id'], en_loc['id']
        if target_id == 'all':
            for comp in translator.get_components(config.site_id):
                if translator.process_component_dom(comp['id'], es_id, en_id, force=True):
                    processed += 1
        else:
            if translator.process_component_dom(target_id, es_id, en_id, force=True):
                processed += 1

    flash(f"Traducción manual finalizada. Nodos/Ítems procesados: {processed}", "success")
    return redirect(url_for('main.manual'))

# ==========================================
# RUTAS DE AUTOMATIZACIÓN (REGLAS)
# ==========================================

@main.route('/auto', methods=['GET', 'POST'])
@login_required
def auto():
    translator, config = get_translator()
    if not translator:
        flash("Configure las APIs en el Dashboard primero.", "warning")
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        target_id = request.form.get('target_id')
        target_type = request.form.get('target_type')
        trigger_type = request.form.get('trigger_type')
        webhook_secret = request.form.get('webhook_secret')
        frequency_days = int(request.form.get('frequency_days', 3)) if trigger_type == 'cron' else 0
        modified_within_days = int(request.form.get('modified_within_days', 5)) if trigger_type == 'cron' else 0
        target_name = request.form.get('target_name', 'Sin nombre')

        existing_rule = AutoRule.query.filter_by(target_id=target_id).first()
        if existing_rule:
            existing_rule.target_type = target_type
            existing_rule.trigger_type = trigger_type
            existing_rule.frequency_days = frequency_days
            existing_rule.modified_within_days = modified_within_days
            existing_rule.target_name = target_name
            existing_rule.webhook_secret = webhook_secret
            existing_rule.is_active = True
            flash(f"Regla actualizada para '{target_name}'.", "success")
        else:
            new_rule = AutoRule(
                target_id=target_id,
                target_type=target_type,
                trigger_type=trigger_type,
                frequency_days=frequency_days,
                modified_within_days=modified_within_days,
                target_name=target_name,
                webhook_secret=webhook_secret,
                is_active=True
            )
            db.session.add(new_rule)
            flash("Regla de automatización guardada.", "success")
        
        db.session.commit()

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
    status = "activada" if rule.is_active else "desactivada"
    flash(f"Regla '{rule.target_name}' {status}.", "success")
    return redirect(url_for('main.auto'))

@main.route('/auto/delete/<int:id>', methods=['POST'])
@login_required
def delete_auto(id):
    rule = AutoRule.query.get_or_404(id)
    db.session.delete(rule)
    db.session.commit()
    flash(f"Regla '{rule.target_name}' eliminada.", "success")
    return redirect(url_for('main.auto'))

# ==========================================
# ENDPOINTS AUTOMÁTICOS (CRON Y WEBHOOKS)
# ==========================================

@main.route('/api/cron/translate', methods=['GET', 'POST'])
def cron_translate():
    auth_header = request.headers.get('Authorization')
    expected_secret = f"Bearer {os.environ.get('CRON_SECRET', 'default_cron_secret')}"
    if auth_header != expected_secret:
        return jsonify({"error": "No autorizado"}), 401

    translator, config = get_translator()
    if not translator:
        return jsonify({"error": "Translator no configurado"}), 500

    es_loc, en_loc = translator.get_locales(config.site_id)
    if not es_loc or not en_loc:
        return jsonify({"error": "Locales no configurados"}), 500

    cron_rules = AutoRule.query.filter_by(trigger_type='cron', is_active=True).all()
    now = datetime.utcnow()
    translated_count = 0

    for rule in cron_rules:
        if rule.target_type == 'collection':
            es_id, en_id = es_loc['cmsLocaleId'], en_loc['cmsLocaleId']
            items = translator.get_items(rule.target_id, es_id) if rule.target_id != 'all' else []
            if rule.target_id == 'all':
                for col in translator.get_collections(config.site_id):
                    for item in translator.get_items(col['id'], es_id):
                        if translator.process_cms_item(col['id'], item, en_id):
                            translated_count += 1
            else:
                for item in items:
                    if translator.process_cms_item(rule.target_id, item, en_id):
                        translated_count += 1

        elif rule.target_type == 'page':
            es_id, en_id = es_loc['id'], en_loc['id']
            if rule.target_id == 'all':
                for page in translator.get_pages(config.site_id):
                    if translator.process_page_dom(page['id'], es_id, en_id):
                        translated_count += 1
            else:
                if translator.process_page_dom(rule.target_id, es_id, en_id):
                    translated_count += 1

        elif rule.target_type == 'component':
            es_id, en_id = es_loc['id'], en_loc['id']
            if rule.target_id == 'all':
                for comp in translator.get_components(config.site_id):
                    if translator.process_component_dom(comp['id'], es_id, en_id):
                        translated_count += 1
            else:
                if translator.process_component_dom(rule.target_id, es_id, en_id):
                    translated_count += 1

        rule.last_run = now
        db.session.commit()

    return jsonify({"status": "Cron finalizado con éxito", "items_traducidos": translated_count})

# ==========================================
# WEBHOOK PRINCIPAL - CON LOGS POR CORREO
# ==========================================

@main.route('/api/webhook/webflow', methods=['POST'])
def webflow_webhook():
    # Obtener configuración para enviar correos
    config = Settings.query.first()
    admin_email = config.admin_email if config else None
    smtp_email = config.smtp_email if config else None
    smtp_password = config.smtp_password if config else None

    # Variables para el log
    log_lines = []
    def add_log(msg):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_lines.append(f"[{timestamp}] {msg}")
        print(f"[{timestamp}] {msg}")

    add_log("=" * 60)
    add_log("📨 NUEVO WEBHOOK RECIBIDO")
    add_log("=" * 60)

    try:
        translator, config = get_translator()
        if not translator:
            add_log("❌ Translator no configurado")
            send_webhook_log(admin_email, smtp_email, smtp_password, "⚠️ Webhook Fallido - Translator no configurado", "\n".join(log_lines))
            return jsonify({"status": "No config"}), 200

        # --- VALIDACIÓN CRIPTOGRÁFICA ---
        signature = request.headers.get('x-webflow-signature')
        timestamp_header = request.headers.get('x-webflow-timestamp')
        add_log(f"🔐 Validación: signature={signature[:20] if signature else 'None'}... timestamp={timestamp_header}")

        active_webhook_rules = AutoRule.query.filter_by(trigger_type='webhook', is_active=True).all()
        secrets = set([r.webhook_secret for r in active_webhook_rules if r.webhook_secret])

        if secrets and signature and timestamp_header:
            msg = f"{timestamp_header}:{request.get_data(as_text=True)}"
            is_valid = False
            for secret in secrets:
                expected_sig = hmac.new(
                    secret.encode('utf-8'),
                    msg.encode('utf-8'),
                    hashlib.sha256
                ).hexdigest()
                if hmac.compare_digest(expected_sig, signature):
                    is_valid = True
                    break
            if not is_valid:
                add_log("❌ Firma inválida. Posible ataque.")
                send_webhook_log(admin_email, smtp_email, smtp_password, "⚠️ Webhook Rechazado - Firma Inválida", "\n".join(log_lines))
                return jsonify({"error": "Firma inválida. Posible ataque."}), 401
            else:
                add_log("✅ Firma verificada correctamente")
        else:
            add_log("ℹ️ Sin validación criptográfica (no hay secrets o headers)")

        data = request.json
        if not data:
            add_log("❌ No se recibió data JSON")
            send_webhook_log(admin_email, smtp_email, smtp_password, "⚠️ Webhook Fallido - Sin Data", "\n".join(log_lines))
            return jsonify({"status": "No data"}), 400

        add_log(f"📦 Payload recibido: {json.dumps(data, indent=2)}")

        es_loc, en_loc = translator.get_locales(config.site_id)
        if not es_loc or not en_loc:
            add_log("❌ No se pudieron obtener los locales")
            send_webhook_log(admin_email, smtp_email, smtp_password, "⚠️ Webhook Fallido - Error de Locales", "\n".join(log_lines))
            return jsonify({"status": "locales error"}), 200

        trigger_type = data.get('triggerType')
        add_log(f"⚡ Trigger Type: {trigger_type}")

        # --- CASO 1: Evento de CMS ---
        if trigger_type in ['collection-item-created', 'collection-item-changed', 
                            'collection-item-published', 'collection-item-unpublished']:
            collection_id = data.get('collectionId') or data.get('_cid')
            item_id = data.get('itemId') or data.get('_id')
            add_log(f"📂 Collection ID: {collection_id}, Item ID: {item_id}")

            if collection_id and item_id:
                rule = AutoRule.query.filter_by(
                    target_type='collection', 
                    trigger_type='webhook', 
                    is_active=True
                ).filter(
                    (AutoRule.target_id == collection_id) | (AutoRule.target_id == 'all')
                ).first()

                if rule:
                    add_log(f"✅ Regla encontrada: {rule.target_name} (ID: {rule.id})")
                    add_log(f"⏳ Esperando 2 segundos para que Webflow procese el cambio...")
                    time.sleep(2)

                    full_item = translator.get_single_item(collection_id, item_id, es_loc['cmsLocaleId'])
                    if full_item:
                        add_log(f"📄 Item obtenido: {full_item.get('id')} - {full_item.get('fieldData', {}).get('name', 'Sin nombre')}")
                        add_log(f"🔄 Traduciendo item con force=True...")
                        
                        success = translator.process_cms_item(collection_id, full_item, en_loc['cmsLocaleId'], force=True)
                        if success:
                            add_log("✅ Item traducido correctamente")
                            add_log(f"🚀 Publicando sitio...")
                            publish_result = translator.publish_site(config.site_id)
                            add_log(f"📤 Resultado de publicación: {publish_result}")
                            
                            # Enviar correo con el log completo
                            send_webhook_log(
                                admin_email, smtp_email, smtp_password,
                                f"✅ Webhook Exitoso - Item {item_id} traducido",
                                "\n".join(log_lines)
                            )
                            
                            return jsonify({
                                "status": f"✅ Item {item_id} traducido y sitio publicado.",
                                "publish": publish_result
                            }), 200
                        else:
                            add_log("⚠️ El item no necesitaba traducción (sin cambios)")
                            send_webhook_log(
                                admin_email, smtp_email, smtp_password,
                                f"ℹ️ Webhook - Item {item_id} sin cambios",
                                "\n".join(log_lines)
                            )
                            return jsonify({"status": f"⏭️ Item {item_id} no necesitaba traducción"}), 200
                    else:
                        add_log(f"❌ Item no encontrado: {item_id}")
                        send_webhook_log(
                            admin_email, smtp_email, smtp_password,
                            f"⚠️ Webhook - Item {item_id} no encontrado",
                            "\n".join(log_lines)
                        )
                        return jsonify({"error": "Item no encontrado"}), 404
                else:
                    add_log(f"⏭️ No hay regla activa para collection {collection_id}")
                    send_webhook_log(
                        admin_email, smtp_email, smtp_password,
                        f"ℹ️ Webhook Ignorado - Sin regla para collection {collection_id}",
                        "\n".join(log_lines)
                    )
                    return jsonify({"status": f"⏭️ No hay regla activa para collection {collection_id}"}), 200
            else:
                add_log(f"⚠️ Faltan collection_id o item_id: collection_id={collection_id}, item_id={item_id}")
                send_webhook_log(
                    admin_email, smtp_email, smtp_password,
                    "⚠️ Webhook Incompleto - Faltan IDs",
                    "\n".join(log_lines)
                )
                return jsonify({"status": "Faltan IDs"}), 200

        # --- CASO 2: Evento de página ---
        elif trigger_type in ['page-created', 'page-metadata-updated', 'page-deleted']:
            page_id = data.get('pageId')
            add_log(f"📄 Page ID: {page_id}")

            if page_id:
                rule = AutoRule.query.filter_by(
                    target_type='page', 
                    trigger_type='webhook', 
                    is_active=True
                ).filter(
                    (AutoRule.target_id == page_id) | (AutoRule.target_id == 'all')
                ).first()

                if rule:
                    add_log(f"✅ Regla encontrada: {rule.target_name} (ID: {rule.id})")
                    add_log(f"⏳ Esperando 2 segundos para que Webflow procese el cambio...")
                    time.sleep(2)

                    add_log(f"🔄 Traduciendo página con force=True...")
                    success = translator.process_page_dom(page_id, es_loc['id'], en_loc['id'], force=True)
                    if success:
                        add_log("✅ Página traducida correctamente")
                        add_log(f"🚀 Publicando sitio...")
                        publish_result = translator.publish_site(config.site_id)
                        add_log(f"📤 Resultado de publicación: {publish_result}")
                        
                        send_webhook_log(
                            admin_email, smtp_email, smtp_password,
                            f"✅ Webhook Exitoso - Página {page_id} traducida",
                            "\n".join(log_lines)
                        )
                        
                        return jsonify({
                            "status": f"✅ Página {page_id} traducida y sitio publicado.",
                            "publish": publish_result
                        }), 200
                    else:
                        add_log("⚠️ La página no necesitaba traducción (sin cambios)")
                        send_webhook_log(
                            admin_email, smtp_email, smtp_password,
                            f"ℹ️ Webhook - Página {page_id} sin cambios",
                            "\n".join(log_lines)
                        )
                        return jsonify({"status": f"⏭️ Página {page_id} no necesitaba traducción"}), 200
                else:
                    add_log(f"⏭️ No hay regla activa para page {page_id}")
                    send_webhook_log(
                        admin_email, smtp_email, smtp_password,
                        f"ℹ️ Webhook Ignorado - Sin regla para page {page_id}",
                        "\n".join(log_lines)
                    )
                    return jsonify({"status": f"⏭️ No hay regla activa para page {page_id}"}), 200
            else:
                add_log(f"⚠️ Falta page_id")
                send_webhook_log(
                    admin_email, smtp_email, smtp_password,
                    "⚠️ Webhook Incompleto - Falta page_id",
                    "\n".join(log_lines)
                )
                return jsonify({"status": "Falta page_id"}), 200

        # --- CASO 3: Publicación de sitio ---
        elif trigger_type == 'site-publish':
            site_id = data.get('siteId')
            add_log(f"🌐 Site Publish: {site_id}")
            if site_id and site_id == config.site_id:
                page_rules = AutoRule.query.filter_by(
                    target_type='page', 
                    trigger_type='webhook', 
                    is_active=True
                ).filter(AutoRule.target_id == 'all').all()

                processed = 0
                for rule in page_rules:
                    add_log(f"📄 Procesando regla de páginas: {rule.target_name}")
                    for page in translator.get_pages(config.site_id):
                        if translator.process_page_dom(page['id'], es_loc['id'], en_loc['id']):
                            processed += 1
                add_log(f"✅ Site Publish: {processed} páginas procesadas.")
                
                send_webhook_log(
                    admin_email, smtp_email, smtp_password,
                    f"✅ Site Publish - {processed} páginas procesadas",
                    "\n".join(log_lines)
                )
                return jsonify({"status": f"Site Publish: {processed} páginas procesadas."}), 200
            else:
                add_log(f"⏭️ Site Publish ignorado (site_id no coincide)")
                return jsonify({"status": "Site Publish ignorado"}), 200

        # --- CASO 4: Evento no soportado ---
        else:
            add_log(f"ℹ️ Trigger '{trigger_type}' no soportado aún")
            send_webhook_log(
                admin_email, smtp_email, smtp_password,
                f"ℹ️ Webhook Ignorado - Trigger no soportado: {trigger_type}",
                "\n".join(log_lines)
            )
            return jsonify({"status": f"Trigger '{trigger_type}' no soportado aún"}), 200

    except Exception as e:
        add_log(f"❌ EXCEPCIÓN: {str(e)}")
        import traceback
        add_log(traceback.format_exc())
        send_webhook_log(
            admin_email, smtp_email, smtp_password,
            f"❌ Webhook con Error - Excepción",
            "\n".join(log_lines)
        )
        return jsonify({"error": str(e)}), 500
