import os
import tempfile
import requests
import deepl
import html
import hashlib
import smtplib
import json
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
        if res.status_code != 200:
            return None, None
        locales = res.json().get('locales', {})
        primary = locales.get('primary', {})
        en_locale = next((l for l in locales.get('secondary', []) if 'en' in l['tag'].lower()), None)
        return primary, en_locale

    # ✅ NUEVO: Método para publicar el sitio
    def publish_site(self, site_id):
        """Publica el sitio en Webflow para que los cambios se reflejen."""
        url = f"{self.base_url}/sites/{site_id}/publish"
        payload = {"publishTo": ["*"]}  # Publica todos los locales
        try:
            res = requests.post(url, headers=self.headers, json=payload)
            if res.status_code in [200, 202]:
                self.escribe_log(f"✅ Sitio {site_id} publicado correctamente.")
                return {"success": True, "message": "Sitio publicado"}
            else:
                self.escribe_log(f"⚠️ Error al publicar sitio: {res.status_code} - {res.text}")
                return {"success": False, "message": res.text}
        except Exception as e:
            self.escribe_log(f"❌ Excepción publicando sitio: {e}")
            return {"success": False, "message": str(e)}

    def generate_hash(self, text_data):
        return hashlib.sha256(str(text_data).encode('utf-8')).hexdigest()

    def can_translate(self, item_id, item_type, data_to_hash, force=False):
        """Verifica si el contenido ha cambiado. Si force=True, siempre traduce."""
        if force:
            # ✅ Traducción forzada: ignora caché y límites
            self.escribe_log(f"🔓 Traducción forzada para {item_id}. Ignorando caché y límites.")
            return True

        record = TranslationRecord.query.filter_by(item_id=item_id).first()
        current_hash = self.generate_hash(data_to_hash)

        if record:
            if record.content_hash == current_hash:
                self.escribe_log(f"⏭️ {item_id}: Sin cambios detectados. No se traduce.")
                return False
            # ✅ Se eliminó el límite de 3 traducciones
            record.content_hash = current_hash
            record.translation_count += 1
            record.last_translated = datetime.utcnow()
        else:
            record = TranslationRecord(
                item_id=item_id,
                item_type=item_type,
                content_hash=current_hash,
                translation_count=1
            )
            db.session.add(record)

        db.session.commit()
        return True

    def translate_text(self, text, is_html=False):
        if not text or not str(text).strip():
            return text
        try:
            if is_html:
                res = self.translator.translate_text(
                    text, source_lang="ES", target_lang="EN-US", tag_handling="html"
                )
            else:
                res = self.translator.translate_text(
                    text, source_lang="ES", target_lang="EN-US"
                )
            return html.unescape(res.text)
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
        """Obtiene un solo ítem del CMS por su ID."""
        res = requests.get(f"{self.base_url}/collections/{collection_id}/items/{item_id}", headers=self.headers, params={"cmsLocaleId": locale_id})
        return res.json() if res.status_code == 200 else None

    def process_cms_item(self, collection_id, item, en_locale_id, force=False):
        """Traduce un solo item del CMS. force=True ignora el caché y límites."""
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

        payload = {
            "items": [{"id": item['id'], "cmsLocaleId": en_locale_id, "fieldData": translated_fields}]
        }
        res = requests.patch(
            f"{self.base_url}/collections/{collection_id}/items?skipInvalidFiles=true",
            headers=self.headers,
            json=payload
        )
        if res.status_code == 200:
            self.escribe_log(f"✅ CMS Item {item['id']} actualizado en inglés.")
            return True
        else:
            self.escribe_log(f"❌ Error actualizando CMS: {res.status_code} - {res.text}")
            return False

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
        """Traduce el DOM de una página. force=True ignora el caché y límites."""
        self.escribe_log(f"\n======================================")
        self.escribe_log(f"Iniciando traducción de DOM ID: '{page_id}'")
        self.escribe_log(f"======================================")

        nodes = self.get_page_dom(page_id, es_locale_id)
        if not nodes:
            self.escribe_log(f"⚠️ No se encontraron nodos para la página {page_id}")
            return False

        if not self.can_translate(page_id, 'page', nodes, force=force):
            self.escribe_log(f"⏭️ Página {page_id}: Sin cambios o límite alcanzado.")
            return False

        try:
            diag_file = os.path.join(self.tmp_dir, "webflow_diagnostico.json")
            with open(diag_file, "w", encoding="utf-8") as f:
                json.dump(nodes, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

        translated_nodes = []
        for node in nodes:
            node_id = node.get("id")
            node_type = node.get("type")
            if not node_id:
                continue

            if node_type == "text" and "text" in node and isinstance(node["text"], dict):
                text_obj = node["text"]
                if "html" in text_obj and text_obj["html"].strip():
                    tr_html = self.translate_text(text_obj["html"], is_html=True)
                    translated_nodes.append({"nodeId": node_id, "text": tr_html})
                    self.escribe_log(f"📝 HTML traducido: {text_obj['html'][:50]}... ➜ {tr_html[:50]}...")
                elif "text" in text_obj and text_obj["text"].strip():
                    tr_text = self.translate_text(text_obj["text"], is_html=False)
                    translated_nodes.append({"nodeId": node_id, "text": tr_text})
                    self.escribe_log(f"📝 Texto traducido: {text_obj['text'][:50]}... ➜ {tr_text[:50]}...")

            elif node_type == "submit-button":
                if "value" in node:
                    translated_nodes.append({"nodeId": node_id, "value": self.translate_text(node["value"])})
                if "waitingText" in node:
                    translated_nodes.append({"nodeId": node_id, "waitingText": self.translate_text(node["waitingText"])})

            elif "propertyOverrides" in node and isinstance(node["propertyOverrides"], dict):
                overrides = node["propertyOverrides"]
                new_overrides = {}
                for p_key, p_val in overrides.items():
                    if isinstance(p_val, str) and len(p_val.strip()) > 0 and (" " in p_val or len(p_val) > 4):
                        new_overrides[p_key] = self.translate_text(p_val)
                    else:
                        new_overrides[p_key] = p_val
                if new_overrides != overrides:
                    translated_nodes.append({"nodeId": node_id, "propertyOverrides": new_overrides})

            elif "attributes" in node and isinstance(node["attributes"], dict):
                attrs = node["attributes"]
                if "placeholder" in attrs and isinstance(attrs["placeholder"], str) and attrs["placeholder"].strip():
                    translated_nodes.append({"nodeId": node_id, "placeholder": self.translate_text(attrs["placeholder"])})

        if not translated_nodes:
            self.escribe_log(f"⚠️ No se encontraron textos para traducir en página {page_id}")
            return False

        if self.update_page_dom(page_id, en_locale_id, translated_nodes):
            self.escribe_log(f"✅ Página {page_id} actualizada con {len(translated_nodes)} nodos traducidos.")
            return True
        else:
            self.escribe_log(f"❌ Error al actualizar página {page_id}")
            return False

    def update_page_dom(self, page_id, locale_id, nodes):
        url = f"{self.base_url}/pages/{page_id}/dom"
        res = requests.post(url, headers=self.headers, params={"localeId": locale_id}, json={"nodes": nodes})
        return res.status_code == 200

    # --- COMPONENTS API ---

    def get_components(self, site_id):
        res = requests.get(f"{self.base_url}/sites/{site_id}/components", headers=self.headers)
        return res.json().get('components', []) if res.status_code == 200 else []

    def get_component_dom(self, component_id, locale_id):
        res = requests.get(f"{self.base_url}/components/{component_id}/dom", headers=self.headers, params={"localeId": locale_id})
        if res.status_code != 200:
            return []
        return res.json().get('nodes', [])

    def process_component_dom(self, component_id, es_locale_id, en_locale_id, force=False):
        """Traduce el DOM de un componente."""
        self.escribe_log(f"\n======================================")
        self.escribe_log(f"Iniciando traducción de Componente ID: '{component_id}'")
        self.escribe_log(f"======================================")

        nodes = self.get_component_dom(component_id, es_locale_id)
        if not nodes:
            return False

        if not self.can_translate(component_id, 'component', nodes, force=force):
            self.escribe_log(f"⏭️ Componente {component_id}: Sin cambios o límite alcanzado.")
            return False

        translated_nodes = []
        for node in nodes:
            node_id = node.get("id")
            node_type = node.get("type")
            if not node_id:
                continue

            if node_type == "text" and "text" in node and isinstance(node["text"], dict):
                text_obj = node["text"]
                if "html" in text_obj and text_obj["html"].strip():
                    tr_html = self.translate_text(text_obj["html"], is_html=True)
                    translated_nodes.append({"nodeId": node_id, "text": tr_html})
                elif "text" in text_obj and text_obj["text"].strip():
                    tr_text = self.translate_text(text_obj["text"], is_html=False)
                    translated_nodes.append({"nodeId": node_id, "text": tr_text})

            elif node_type == "submit-button":
                if "value" in node:
                    translated_nodes.append({"nodeId": node_id, "value": self.translate_text(node["value"])})
                if "waitingText" in node:
                    translated_nodes.append({"nodeId": node_id, "waitingText": self.translate_text(node["waitingText"])})

            elif "propertyOverrides" in node and isinstance(node["propertyOverrides"], dict):
                overrides = node["propertyOverrides"]
                new_overrides = {}
                for p_key, p_val in overrides.items():
                    if isinstance(p_val, str) and len(p_val.strip()) > 0 and (" " in p_val or len(p_val) > 4):
                        new_overrides[p_key] = self.translate_text(p_val)
                    else:
                        new_overrides[p_key] = p_val
                if new_overrides != overrides:
                    translated_nodes.append({"nodeId": node_id, "propertyOverrides": new_overrides})

            elif "attributes" in node and isinstance(node["attributes"], dict):
                attrs = node["attributes"]
                if "placeholder" in attrs and isinstance(attrs["placeholder"], str) and attrs["placeholder"].strip():
                    translated_nodes.append({"nodeId": node_id, "placeholder": self.translate_text(attrs["placeholder"])})

        if not translated_nodes:
            return False

        if self.update_component_dom(component_id, en_locale_id, translated_nodes):
            self.escribe_log(f"✅ Componente {component_id} actualizado.")
            return True
        return False

    def update_component_dom(self, component_id, locale_id, nodes):
        url = f"{self.base_url}/components/{component_id}/dom"
        res = requests.post(url, headers=self.headers, params={"localeId": locale_id}, json={"nodes": nodes})
        return res.status_code == 200
