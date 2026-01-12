# hvostiki-backend

1. Создать в корне файл .env со следующим содержимым
```
DEBUG=1
SECRET_KEY=dev-secret

POSTGRES_DB=app
POSTGRES_USER=app
POSTGRES_PASSWORD=app

DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0

UCALLER_SERVICE_ID=
UCALLER_API_KEY=
```
2. Выполнить `docker-compose -f docker-compose.dev.yml up --build`