import logging
import requests
import os

logger = logging.getLogger("onpar_notifications")

# OneSignal veya Firebase REST API Entegrasyonu (Sıfır ek kütüphane bağımlılığı ile ultra hızlı)
ONESIGNAL_APP_ID = os.environ.get("ONESIGNAL_APP_ID", "")
ONESIGNAL_REST_KEY = os.environ.get("ONESIGNAL_REST_KEY", "")

def send_b2b_push_notification(title: str, body: str, data: dict = None):
    """
    Arka planda (FastAPI BackgroundTasks) çalışan non-blocking bildirim motoru.
    """
    logger.info(f"[PUSH BİLDİRİMİ] Başlık: {title} | Mesaj: {body}")
    
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_KEY:
        # OneSignal anahtarları henüz girilmemişse konsola enterprise log basar
        return

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Basic {ONESIGNAL_REST_KEY}"
    }
    payload = {
        "app_id": ONESIGNAL_APP_ID,
        "included_segments": ["Subscribed Users"],
        "headings": {"en": title, "tr": title},
        "contents": {"en": body, "tr": body},
        "data": data or {}
    }
    try:
        requests.post("https://onesignal.com/api/v1/notifications", headers=headers, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Push bildirim iletim hatası: {e}")