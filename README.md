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

## Прод: деплой

Прод-стек — `docker-compose.prod.yml`: nginx (80/443, статика, медиа) → gunicorn, плюс `celery`,
`celery-beat`, `postgres`, `redis`, `rabbitmq`. Наружу открыты только 80 и 443, базы доступны лишь
внутри сети compose. Миграции и `collectstatic` выполняет `entrypoint.sh` при старте `web`
(в `celery`/`celery-beat` они выключены через `RUN_MIGRATIONS=0`).

### Что нужно на сервере

1. Docker с плагином compose, домен, направленный A-записью на сервер.
2. `.env` из шаблона `.env.prod.example`:
   ```
   cp .env.prod.example .env
   ```
   Обязательно заполнить: `SECRET_KEY` (`python -c "import secrets; print(secrets.token_urlsafe(64))"`),
   `POSTGRES_PASSWORD`, `RABBITMQ_DEFAULT_PASS` (и тот же пароль внутри `CELERY_BROKER_URL`),
   `DJANGO_ALLOWED_HOSTS`, `SERVER_NAME`, `CSRF_TRUSTED_ORIGINS`, `UCALLER_SERVICE_ID`/`UCALLER_API_KEY`.
   Проверить, что `DEBUG=0` и `SERVE_MEDIA=0` (медиа отдаёт nginx).
3. Ключ Firebase рядом с проектом (он монтируется томом, а не копируется в образ):
   ```
   mkdir -p secrets && cp firebase-key.json secrets/firebase-key.json
   ```
4. Запуск:
   ```
   docker compose -f docker-compose.prod.yml up -d --build
   docker compose -f docker-compose.prod.yml ps      # все сервисы healthy
   curl -fsS http://127.0.0.1/healthz                # {"status": "ok"}
   ```
5. Суперпользователь для админки:
   ```
   docker compose -f docker-compose.prod.yml exec web manage createsuperuser
   ```
   (`manage` — режим entrypoint: команда выполняется от пользователя приложения.)

### DNS и адреса

Нужны **обе** DNS-записи на домен: `A` → публичный IPv4 и `AAAA` → публичный IPv6.

**IPv4 обязателен**, и дело не только в клиентах:

- `api.ucaller.ru` (звонок с кодом подтверждения — единственный способ входа) **не имеет AAAA-записи**,
  поэтому сервер без IPv4 не сможет даже отправить код;
- часть мобильных сетей отдаёт клиентам только IPv4, и такие клиенты до сервера с одним IPv6 не доедут.

nginx слушает обе семьи адресов (`listen 80` + `listen [::]:80`), compose публикует порты в обеих
(`80:80` и `[::]:80:80`). Если у провайдера IPv4 выдаётся как платная опция, её всё равно стоит взять:
без неё ломается вход в приложение.

### HTTPS (Let's Encrypt)

Сертификата при первом запуске ещё нет, поэтому в `nginx/conf.d/default.conf.template` блок
`listen 443 ssl` закомментирован — сначала поднимаем HTTP, выпускаем сертификат, затем включаем HTTPS:

```
mkdir -p certbot/conf certbot/www
docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot -d api.example.com --email you@example.com --agree-tos --no-eff-email
```

Далее раскомментировать блок `# --- HTTPS ---` в шаблоне (там `SERVER_NAME` подставится в пути
к сертификату) и перезапустить nginx:

```
docker compose -f docker-compose.prod.yml restart nginx
```

Продление — по крону сервера (сертификат живёт 90 дней):
```
docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew --webroot -w /var/www/certbot
docker compose -f docker-compose.prod.yml restart nginx
```

### Обновление версии

```
git pull
docker compose -f docker-compose.prod.yml up -d --build   # entrypoint применит миграции
```

Миграции, меняющие данные (`0018_event_time_to_local`, `0029_notification_categories_by_event_group`),
применяются вместе с выкаткой кода: короткое окно, когда API ещё на старых ключах категорий.

### Сколько ресурсов нужно

Замерено на живом стеке (7 контейнеров, `GUNICORN_WORKERS=2`, `CELERY_CONCURRENCY=1`, настройки
Postgres/Redis из `docker-compose.prod.yml`):

| Сервис | Память в покое |
|---|---|
| web (gunicorn, 2 воркера) | ~145 МБ |
| celery worker | ~115 МБ |
| rabbitmq | ~125 МБ |
| celery-beat | ~80 МБ |
| postgres | ~20 МБ |
| redis | ~10 МБ |
| nginx | ~7 МБ |
| **Итого** | **~500 МБ** |

**Минимум для старта — 1 vCPU / 2 ГБ RAM / 20 ГБ SSD.** На одном ядре API отвечает с медианой
1–6 мс, 40 параллельных запросов к ручке с запросами в БД проходят за 0,14 с (≈280 req/s),
задачи Celery обрабатываются, OOM не возникает: запас по памяти примерно четырёхкратный.

Что важно помнить:

- `CELERY_CONCURRENCY` по умолчанию 2; на 2 ГБ держите 1–2. Пул prefork плодит процессы
  (по умолчанию «ядра + 1») и на маленьком сервере съедает больше памяти, чем всё остальное вместе.
- Postgres на старте лёгкий, потому что база пустая. Когда БД вырастет, память начнут забирать
  page cache и `shared_buffers` (сейчас 128 МБ) — узкое место по памяти появится именно там.
- Дамп базы (`pg_dump`) и `collectstatic` при деплое дают кратковременный всплеск CPU и памяти:
  на 1 vCPU деплой ощутимо заметен пользователям, но не падает.
- При росте нагрузки (больше пары сотен активных пользователей, тяжёлые отчёты по сериям событий,
  много медиа) апгрейд до 2 vCPU / 4 ГБ делается перезапуском на новом тарифе — данные в томах,
  переезд сводится к переносу томов или восстановлению из дампа.

### Резервные копии

Данные лежат в именованных томах compose. Минимум — ежедневный дамп базы и копия медиа:

```
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > /backup/hvostiki-$(date +%F).sql.gz
docker run --rm -v hvostiki-backend_media:/data -v /backup:/backup alpine \
  tar czf /backup/media-$(date +%F).tar.gz -C /data .
```

Дампы и архивы хранить вне сервера (S3-совместимое хранилище) — иначе смысла в них мало.
Восстановление: `zcat dump.sql.gz | docker compose -f docker-compose.prod.yml exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`.

### Что стоит вынести в следующие итерации

- объектное хранилище для медиа (`MEDIA_STORAGE_BACKEND=storages.backends.s3.S3Storage` + `django-storages`);
- внешний мониторинг `/healthz` и сбор логов (сейчас логи контейнеров никуда не отправляются);
- защита админки от перебора пароля (`django-axes`).
