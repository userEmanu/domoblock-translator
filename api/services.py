import os
import tempfile
import requests
import deepl
import html
import hashlib
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from api.models import db, TranslationRecord

def send_login_alert(target_email, smtp_email, smtp_password):
    if not smtp_email or not smtp_password:
        print("Configuración SMTP incompleta. Correo no enviado.")
        return

    msg = MIMEText(f"Se ha detectado un nuevo inicio de sesión exitoso en el panel administrativo de traducciones a las {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC.")
    msg['Subject'] = 'Alerta de Seguridad: Nuevo Ingreso (Domoblock Translator)'
    msg['From'] = smtp_email
    msg['To'] = target_email

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
    except Exception as e:
        print(f"Error enviando correo de seguridad: {e}")

class TranslatorService:
    def __init__(self, webflow_token, deepl_key):
        self.base_url = "https://api.webflow.com/v2"
        self.headers = {
            "Authorization": f"Bearer {webflow_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        self.translator = deepl.Translator(deepl_key)
        self.tmp_dir = tempfile.gettempdir()
        self.log_filename = os.path.join(self.tmp_dir, "webflow_translator.log")

    def escribe_log(self, mensaje, mostrar_consola=True):
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        linea_log = f"[{timestamp}] {mensaje}"
        try:
            with open(self.log_filename, "a", encoding="utf-8") as archivo_log:
                archivo_log.write(linea_log + "\n")
        except Exception:
            pass
        if mostrar_consola:
            print(mensaje)

    def get_deepl_usage(self):
        try:
            usage = self.translator.get_usage()
            if usage.character.limit:
                return f"{usage.character.count} / {usage.character.limit} caracteres usados"
            return f"{usage.character.count} caracteres consumidos"
        except Exception:
            return "No se pudo obtener el consumo. Verifique su API Key."

    def get_locales(self, site_id):
        res = requests.get(f"{self.base_url}/sites/{site_id}", headers=self.headers)
        if res.status_code != 200: return None, None
        locales = res.json().get('locales', {})
        primary = locales.get('primary', {})
        en_locale = next((l for l in locales.get('secondary', []) if 'en' in l['tag'].lower()), None)
        return primary, en_locale

    def generate_hash(self, text_data):
        return hashlib.sha256(str(text_data).encode('utf-8')).hexdigest()

    def can_translate(self, item_id, item_type, data_to_hash, force=False):
        record = TranslationRecord.query.filter_by(item_id=item_id).first()
        current_hash = self.generate_hash(data_to_hash)

        if record:
            if not force:
                if record.content_hash == current_hash:
                    return False 
                if item_type == 'page' and record.translation_count >= 3:
                    return False 
            record.content_hash = current_hash
            record.translation_count += 1
            record.last_translated = datetime.utcnow()
        else:
            record = TranslationRecord(item_id=item_id, item_type=item_type, content_hash=current_hash, translation_count=1)
            db.session.add(record)
        
        db.session.commit()
        return True

    def translate_text(self, text, is_html=False):
        if not text or not str(text).strip(): return text
        try:
            if is_html:
                res = self.translator.translate_text(text, source_lang="ES", target_lang="EN-US", tag_handling="html")
            else:
                res = self.translator.translate_text(text, source_lang="ES", target_lang="EN-US")
            return res.text
        except Exception as e:
            self.escribe_log(f"⚠️ Error DeepL: {e}")
            return text

    # --- CMS API ---
    def get_collections(self, site_id):
        res = requests.get(f"{self.base_url}/sites/{site_id}/collections", headers=self.headers)
        return res.json().get('collections', []) if res.status_code == 200 else []

    def get_items(self, collection_id, locale_id):
        res = requests.get(f"{self.base_url}/collections/{collection_id}/items", headers=self.headers, params={"cmsLocaleId": locale_id})
        return res.json().get('items', []) if res.status_code == 200 else []

    def get_single_item(self, collection_id, item_id, locale_id):
        res = requests.get(f"{self.base_url}/collections/{collection_id}/items/{item_id}", headers=self.headers, params={"cmsLocaleId": locale_id})
        return res.json() if res.status_code == 200 else None

    def process_cms_item(self, collection_id, item, en_locale_id, force=False):
        if not self.can_translate(item['id'], 'collection', item.get('fieldData', {}), force=force):
            return False

        translated_fields = {}
        for key, value in item['fieldData'].items():
            if isinstance(value, str) and key not in ['slug', 'color', 'name']:
                es_html = "<" in value and ">" in value
                tr_val = self.translate_text(value, is_html=es_html)
                tr_val = html.unescape(tr_val)
                translated_fields[key] = tr_val
                self.escribe_log(f"📝 CMS Original: {value[:40]}\n   ➜ DeepL: {tr_val[:40]}")
            else:
                translated_fields[key] = value

        payload = {"items": [{"id": item['id'], "cmsLocaleId": en_locale_id, "fieldData": translated_fields}]}
        res = requests.patch(f"{self.base_url}/collections/{collection_id}/items?skipInvalidFiles=true", headers=self.headers, json=payload)
        return res.status_code == 200

    # --- PAGES API ---
    def get_pages(self, site_id):
        res = requests.get(f"{self.base_url}/sites/{site_id}/pages", headers=self.headers)
        return res.json().get('pages', []) if res.status_code == 200 else []

    def get_page_dom(self, page_id, locale_id):
        res = requests.get(f"{self.base_url}/pages/{page_id}/dom", headers=self.headers, params={"localeId": locale_id})
        if res.status_code != 200:
            self.escribe_log(f"❌ Error leyendo la página en Webflow: {res.text}")
            return []
        return res.json().get('nodes', [])

    def process_page_dom(self, page_id, es_locale_id, en_locale_id, force=False):
        self.escribe_log(f"\n======================================")
        self.escribe_log(f"Iniciando traducción de DOM ID: '{page_id}'")
        self.escribe_log(f"======================================")
        
        nodes = self.get_page_dom(page_id, es_locale_id)
        if not nodes: return False
        
        if not self.can_translate(page_id, 'page', nodes, force=force):
            self.escribe_log(f"⚠️ No se encontraron cambios o se alcanzó el límite. (Uso manual fuerza la traducción).")
            return False

        try:
            diag_file = os.path.join(self.tmp_dir, "webflow_diagnostico.json")
            with open(diag_file, "w", encoding="utf-8") as f:
                import json
                json.dump(nodes, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

        translated_nodes = []
        self.escribe_log(f"Analizando {len(nodes)} nodos estructurales...", mostrar_consola=True)

        for node in nodes:
            node_id = node.get("id")
            node_type = node.get("type")
            if not node_id: continue
                
            if node_type == "text" and "text" in node and isinstance(node["text"], dict):
                text_obj = node["text"]
                if "html" in text_obj and text_obj["html"].strip():
                    original_html = text_obj["html"]
                    tr_html = self.translate_text(original_html, is_html=True)
                    tr_html = html.unescape(tr_html)
                    translated_nodes.append({"nodeId": node_id, "text": tr_html})
                elif "text" in text_obj and text_obj["text"].strip():
                    original_text = text_obj["text"]
                    tr_text = self.translate_text(original_text, is_html=False)
                    tr_text = html.unescape(tr_text)
                    translated_nodes.append({"nodeId": node_id, "text": tr_text})
            
            elif node_type == "submit-button":
                if "value" in node:
                    tr_val = self.translate_text(node["value"], is_html=False)
                    tr_val = html.unescape(tr_val)
                    translated_nodes.append({"nodeId": node_id, "value": tr_val})
                if "waitingText" in node:
                    wait_val = self.translate_text(node["waitingText"], is_html=False)
                    wait_val = html.unescape(wait_val)
                    translated_nodes.append({"nodeId": node_id, "waitingText": wait_val})
                
            elif "propertyOverrides" in node and isinstance(node["propertyOverrides"], dict):
                overrides = node["propertyOverrides"]
                new_overrides = {}
                modificado = False
                for p_key, p_val in overrides.items():
                    if isinstance(p_val, str) and len(p_val.strip()) > 0 and (" " in p_val or len(p_val) > 4):
                        tr_override = self.translate_text(p_val, is_html=False)
                        tr_override = html.unescape(tr_override)
                        new_overrides[p_key] = tr_override
                        modificado = True
                    else:
                        new_overrides[p_key] = p_val
                if modificado:
                    translated_nodes.append({"nodeId": node_id, "propertyOverrides": new_overrides})
                    
            elif "attributes" in node and isinstance(node["attributes"], dict):
                attrs = node["attributes"]
                if "placeholder" in attrs and isinstance(attrs["placeholder"], str) and attrs["placeholder"].strip():
                    tr_place = self.translate_text(attrs["placeholder"], is_html=False)
                    tr_place = html.unescape(tr_place)
                    translated_nodes.append({"nodeId": node_id, "placeholder": tr_place})

        if not translated_nodes:
            self.escribe_log(f"⚠️ No se encontraron textos válidos para traducir.")
            return False
        
        self.escribe_log(f"Subiendo {len(translated_nodes)} nodos completamente traducidos hacia Webflow...")
        url = f"{self.base_url}/pages/{page_id}/dom"
        res = requests.post(url, headers=self.headers, params={"localeId": en_locale_id}, json={"nodes": translated_nodes})
        if res.status_code == 200:
            self.escribe_log(f"✅ Éxito absoluto en la inyección de DOM.")
            return True
        else:
            self.escribe_log(f"❌ Fallo al actualizar DOM. Error: {res.text}")
            return False

    # --- COMPONENTS API ---
    def get_components(self, site_id):
        res = requests.get(f"{self.base_url}/sites/{site_id}/components", headers=self.headers)
        return res.json().get('components', []) if res.status_code == 200 else []

    def get_component_dom(self, component_id, locale_id):
        res = requests.get(f"{self.base_url}/components/{component_id}/dom", headers=self.headers, params={"localeId": locale_id})
        return res.json().get('nodes', []) if res.status_code == 200 else []
        
    def process_component_dom(self, component_id, es_locale_id, en_locale_id, force=False):
        nodes = self.get_component_dom(component_id, es_locale_id)
        if not nodes: return False
        if not self.can_translate(component_id, 'component', nodes, force=force): return False

        translated_nodes = []
        for node in nodes:
            node_id = node.get("id")
            node_type = node.get("type")
            if not node_id: continue
                
            if node_type == "text" and "text" in node and isinstance(node["text"], dict):
                text_obj = node["text"]
                if "html" in text_obj and text_obj["html"].strip():
                    tr_html = self.translate_text(text_obj["html"], is_html=True)
                    tr_html = html.unescape(tr_html)
                    translated_nodes.append({"nodeId": node_id, "text": tr_html})
                elif "text" in text_obj and text_obj["text"].strip():
                    tr_text = self.translate_text(text_obj["text"], is_html=False)
                    tr_text = html.unescape(tr_text)
                    translated_nodes.append({"nodeId": node_id, "text": tr_text})
            
            elif node_type == "submit-button":
                if "value" in node:
                    tr_val = self.translate_text(node["value"], is_html=False)
                    tr_val = html.unescape(tr_val)
                    translated_nodes.append({"nodeId": node_id, "value": tr_val})
                if "waitingText" in node:
                    wait_val = self.translate_text(node["waitingText"], is_html=False)
                    wait_val = html.unescape(wait_val)
                    translated_nodes.append({"nodeId": node_id, "waitingText": wait_val})
                
            elif "propertyOverrides" in node and isinstance(node["propertyOverrides"], dict):
                overrides = node["propertyOverrides"]
                new_overrides = {}
                modificado = False
                for p_key, p_val in overrides.items():
                    if isinstance(p_val, str) and len(p_val.strip()) > 0 and (" " in p_val or len(p_val) > 4):
                        tr_override = self.translate_text(p_val, is_html=False)
                        tr_override = html.unescape(tr_override)
                        new_overrides[p_key] = tr_override
                        modificado = True
                    else:
                        new_overrides[p_key] = p_val
                if modificado:
                    translated_nodes.append({"nodeId": node_id, "propertyOverrides": new_overrides})
                    
            elif "attributes" in node and isinstance(node["attributes"], dict):
                attrs = node["attributes"]
                if "placeholder" in attrs and isinstance(attrs["placeholder"], str) and attrs["placeholder"].strip():
                    tr_place = self.translate_text(attrs["placeholder"], is_html=False)
                    tr_place = html.unescape(tr_place)
                    translated_nodes.append({"nodeId": node_id, "placeholder": tr_place})

        if not translated_nodes: return False
        url = f"{self.base_url}/components/{component_id}/dom"
        res = requests.post(url, headers=self.headers, params={"localeId": en_locale_id}, json={"nodes": translated_nodes})
        return res.status_code == 200
