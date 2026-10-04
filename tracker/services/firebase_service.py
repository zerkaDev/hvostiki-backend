import logging
import os

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    HAS_FIREBASE = True
except ImportError:
    HAS_FIREBASE = False

from django.conf import settings

logger = logging.getLogger(__name__)


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

        key_path = settings.FIREBASE_CREDENTIALS_FILE
        if os.path.exists(key_path):
            try:
                cred = credentials.Certificate(key_path)
                firebase_admin.initialize_app(cred)
                self._initialized = True
            except Exception as e:
                logger.error('Error initializing Firebase: %s', e)
        else:
            logger.warning('Firebase key not found at %s, push-уведомления отключены', key_path)

    def send_push_notification(self, token, title, body, data=None):
        """Отправляет push на один токен.

        Токен, который FCM признал недействительным (приложение удалено, токен сменился),
        удаляется из БД, а вызов возвращает ``None``. Остальные ошибки (недоступность FCM,
        квота и т. п.) пробрасываются, чтобы вызывающий код мог повторить отправку позже.
        """
        if not self._initialized or not HAS_FIREBASE:
            logger.warning('Firebase not initialized or library missing. Cannot send push.')
            return None

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            # FCM принимает в data только строки
            data={key: str(value) for key, value in (data or {}).items()},
            token=token,
            # Напоминания привязаны к моменту времени: доставляем сразу и со звуком
            android=messaging.AndroidConfig(priority='high'),
            apns=messaging.APNSConfig(
                headers={'apns-priority': '10'},
                payload=messaging.APNSPayload(aps=messaging.Aps(sound='default')),
            ),
        )

        try:
            return messaging.send(message)
        except (messaging.UnregisteredError, messaging.SenderIdMismatchError):
            self._forget_token(token)
            return None
        except Exception:
            logger.exception('Error sending Firebase message')
            raise

    @staticmethod
    def _forget_token(token):
        from tracker.models import FCMDevice

        deleted, _ = FCMDevice.objects.filter(fcm_token=token).delete()
        logger.info('FCM-токен недействителен и удалён (записей: %s)', deleted)


firebase_service = FirebaseService()
