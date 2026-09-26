# AGENTS.md

Инструкции для AI-агентов и новых разработчиков, работающих в этом репозитории.

## Что это за проект

Backend приложения для учёта питомцев («Хвостики»).

- **Django 6** + **DRF**, схемы API через **drf-spectacular** (`/api/docs/`)
- **Аутентификация**: JWT (`djangorestframework-simplejwt`), логин по номеру телефона через звонок-код (uCaller)
- **Фоновые задачи**: Celery + RabbitMQ, результат — Redis; рассылка push через Firebase Cloud Messaging
- **БД**: PostgreSQL 16, кеш/рейт-лимиты — Redis
- **Зависимости**: Poetry (`pyproject.toml` + `poetry.lock`), Python >= 3.13

## Быстрый старт

1. Создать `.env` в корне (шаблон — в `README.md`).
2. `make up` — поднять `web`, `postgres`, `redis`, `rabbitmq`, `celery`, `celery-beat`.
3. `make migrate` (или `make mm`) — применить миграции.

**Локально в системе зависимостей нет** — всё запускается внутри контейнера. Все цели `make` построены на
`docker compose -f docker-compose.dev.yml exec web ...`, поэтому сначала должен быть выполнен `make up`.
После изменения зависимостей нужно `make build`.

Полезное: `make help`, `make logs`, `make shell`, `make test args="-k auth"`, `make su`.

## Карта кода

```
config/          settings.py, urls.py, celery.py, wsgi/asgi
tracker/
  models.py      User (UUID pk, логин = phone_number), Breed, Pet,
                 RecurrenceRule, Event, EventCompletion,
                 EventNotificationLog, FCMDevice
  serializers.py DRF-сериализаторы
  schemas.py     drf-spectacular схемы (документация эндпоинтов)
  views.py       APIView / ModelViewSet'ы
  urls.py        маршруты (auth/*, profile/, breeds/, pets/, event_schedule/, devices/register/)
  tasks.py       Celery-задачи (send_confirmation_code, send_event_notifications,
                 flush_expired_tokens)
  services/      firebase_service.py (FCM), ucalles_service.py (звонки-коды)
  utils.py       generate_occurrences, shift_time_by_minutes, normalize_phone
  backends.py    PhoneBackend (вход в Django Admin по телефону)
  tests/         pytest: conftest.py + test_auth/test_pets/test_events/test_notifications/test_profile
  migrations/    0001..0016
```

## Соглашения проекта

1. **Схемы API обязательны.** Для каждого нового вью/экшена добавляй `@extend_schema` в `tracker/schemas.py`
   (теги и описания — на русском). Проверка: `python manage.py spectacular --file /tmp/schema.yml`.
2. **Ошибки** возвращаются как `{'detail': '...'}`; тексты для пользователя — на русском.
3. **Время.** `Event.time` в БД хранится в локальном времени пользователя (`UTC + timezone_offset`, смещение в
   минутах). API принимает и отдаёт время в UTC. Пересчёт — только через `shift_time_by_minutes`.
4. **Права.** По умолчанию `IsAuthenticated` (см. `REST_FRAMEWORK` в `config/settings.py`); публичные вьюхи
   (`SendCodeView`, `VerifyCodeView`, `RefreshTokenView`) явно ставят `permissions.AllowAny`.
5. **JWT.** Ротация refresh-токенов отключена (`ROTATE_REFRESH_TOKENS=False`), поэтому `/auth/token/refresh/`
   возвращает тот же refresh-токен и не продлевает сессию. Отзыв — только
   `POST /auth/logout/` с телом `{"refresh": "..."}`; токен должен принадлежать текущему пользователю
   (иначе 403), повторный вызов идемпотентен (200). Для этого подключено приложение
   `rest_framework_simplejwt.token_blacklist` (требует миграций), а `flush_expired_tokens` чистит
   просроченные записи.
6. **Только отправленный код подтверждения.** В проде `DEBUG_CONFIRMATION_CODE` не задаётся, поэтому
   фиксированного кода `1234` нет; в dev он включается переменной окружения (см. README).
7. **Чтение связанных объектов.** Сериализаторы добавляют «развёрнутые» поля (`pet_obj`, `breed_obj`), это часть
   контракта с мобильным клиентом — не удалять.
8. **Секреты.** `.env` и `firebase-key.json` в `.gitignore` — никогда не коммитить. Push не работает, если
   `firebase-key.json` отсутствует в корне проекта.

## Тесты и проверки

CI в репозитории нет, проверки запускаются руками (нужен `make up`):

```bash
docker compose -f docker-compose.dev.yml run --rm web pytest -q              # все тесты (БД test_app)
docker compose -f docker-compose.dev.yml run --rm web pytest -q --create-db  # пересоздать тестовую БД
docker compose -f docker-compose.dev.yml run --rm web python manage.py check
docker compose -f docker-compose.dev.yml run --rm web python manage.py makemigrations --check --dry-run
docker compose -f docker-compose.dev.yml run --rm web python manage.py spectacular --file /tmp/schema.yml
```

- В `pytest.ini` включён `--reuse-db`: если менялись модели/`INSTALLED_APPS`, добавь `--create-db`.
- Фикстуры в `tracker/tests/conftest.py`: `api_client`, `user`, `auth_client`, `breed`, `pet`, `event`.
  Для авторизации — `auth_client` (через `force_authenticate`) или `api_client.credentials(...)`.
- Внешние сервисы в тестах не дёргаем: `patch('tracker.tasks.send_confirmation_code.delay')`,
  Celery-задачи вызываются напрямую (`send_event_notifications()`), время мокается через
  `patch('django.utils.timezone.now')`.
- Любое изменение поведения API сопровождай тестом. Для задач из `CELERY_BEAT_SCHEDULE` есть регрессионный
  тест `test_beat_schedule_references_registered_tasks` — он ловит опечатки в именах задач.
- Линтеров и форматтеров в проекте нет (black удалён) — стиль держим руками, по образцу соседнего кода.

## Известные грабли и техдолг

- **`Event.done` — legacy**: фактический статус выполнения хранится в `EventCompletion`, поле `done` в API
  вычисляется (`SerializerMethodField`). Поле в БД осталось для совместимости.
- **`send_event_notifications`** обходит все события каждую минуту (SQL-фильтра по дате нет) — на больших
  объёмах стоит добавить предварительную выборку. Окно срабатывания — `NOTIFICATION_LOOKBACK` (2 минуты),
  повторные отправки отсекает `EventNotificationLog`.
- **`token_blacklist`** растёт: чистка висит на задаче `flush_expired_tokens` в beat, а `celery-beat`
  обязателен и в проде (`docker-compose.prod.yml`).
- **Уведомления** рассылаются только тем устройствам, что зарегистрированы через `POST /devices/register/`;
  без `firebase-key.json` отправка молча пропускается (в логах warning).
- Админка: вход staff-пользователя без пароля с пустым паролем разрешён только при `DEBUG=1`.

## Правила работы для агентов

- Минимальные диффы в стиле окружающего кода; не переформатировать и не «улучшать» файлы, не относящиеся к задаче.
- Пользовательские тексты — на русском, как в существующем коде.
- Миграции не править задним числом — только добавлять новые (`make mm`).
- Новые зависимости — в `pyproject.toml` (Poetry) + обновлённый `poetry.lock` (`poetry lock`, версии
  остальных пакетов при этом не поднимаются).
- Не коммитить `.env`, `firebase-key.json`, `celerybeat-schedule`, `media/`.
- Перед завершением задачи: `pytest` + `manage.py check` + `makemigrations --check --dry-run`.
