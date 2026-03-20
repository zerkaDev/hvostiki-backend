import os
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    HAS_FIREBASE = True
except ImportError:
    HAS_FIREBASE = False

from django.conf import settings

class FirebaseService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized or not HAS_FIREBASE:
            return
        
        key_path = os.path.join(settings.BASE_DIR, 'firebase-key.json')
        if os.path.exists(key_path):
            try:
                cred = credentials.Certificate(key_path)
                firebase_admin.initialize_app(cred)
                self._initialized = True
            except Exception as e:
                print(f"Error initializing Firebase: {e}")
        else:
            print(f"Firebase key not found at {key_path}")

    def send_push_notification(self, token, title, body, data=None):
        if not self._initialized or not HAS_FIREBASE:
            print("Firebase not initialized or library missing. Cannot send push.")
            return None

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=token,
        )

        try:
            response = messaging.send(message)
            return response
        except Exception as e:
            print(f"Error sending Firebase message: {e}")
            return None

firebase_service = FirebaseService()
