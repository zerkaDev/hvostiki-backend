# hvostiki-backend

Backend приложения для учёта питомцев: Django + DRF, JWT, Celery, PostgreSQL, Redis, FCM.

## Запуск

1. Создать в корне файл .env со следующим содержимым
```
DEBUG=1
SECRET_KEY=dev-secret
# Код подтверждения для локальной разработки (работает только при DEBUG=1)
DEBUG_CONFIRMATION_CODE=1234

POSTGRES_DB=app
POSTGRES_USER=app
POSTGRES_PASSWORD=app

DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0

UCALLER_SERVICE_ID=
UCALLER_API_KEY=
```
2. Выполнить `make up` (или `docker-compose -f docker-compose.dev.yml up --build`)
3. Применить миграции: `make migrate`

## Полезные команды

- `make help` — список всех команд
- `make test` — запустить тесты внутри контейнера
- `make logs` / `make shell` / `make mm` / `make su`
- Swagger: `http://localhost:8000/api/docs/`

## Важно для продакшена

- `.env` и `firebase-key.json` не коммитятся и должны быть переданы на сервер отдельно
- `DEBUG` обязательно `0`: при `DEBUG=1` включается фиксированный код подтверждения
  `DEBUG_CONFIRMATION_CODE` и отладочные страницы
- `firebase-key.json` (сервисный аккаунт Firebase: Project settings → Service accounts → Generate new private key)
  кладётся в корень проекта или путь к нему задаётся в `FIREBASE_CREDENTIALS_FILE`, иначе push-уведомления
  молча не отправляются
- Не забыть `python manage.py migrate`: refresh-токены используют приложение
  `token_blacklist` (его таблицы создаются миграциями)
- Планировщик `celery-beat` обязателен — на нём висят напоминания о событиях
  и очистка просроченных токенов, а также ежедневная чистка истории «Центра уведомлений»
  (`cleanup_old_notifications`: записи старше `NOTIFICATION_RETENTION_DAYS`, по умолчанию 180 дней)
