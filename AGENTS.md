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
  recurrence.py  движок повторений (чистые функции): якорь, clamp, -1, yearly, until, normalize_rule
  utils.py       generate_occurrences (тонкая обёртка над движком), shift_time_by_minutes, normalize_phone
  time_contract.py  режимы контракта времени (utc/legacy), конвертация, учёт использования
  middleware.py  TimeContractMiddleware (режим, заголовок ответа, лог для метрики)
  backends.py    PhoneBackend (вход в Django Admin по телефону)
  tests/         pytest: conftest.py + test_auth/test_pets/test_events/test_notifications/test_profile/test_time_contract/test_recurrence_*
  tests/data/recurrence_vectors.json  общие векторы повторений (те же, что в мобильном приложении)
  migrations/    0001..0019
```

## Соглашения проекта

1. **Схемы API обязательны.** Для каждого нового вью/экшена добавляй `@extend_schema` в `tracker/schemas.py`
   (теги и описания — на русском). Проверка: `python manage.py spectacular --file /tmp/schema.yml`.
2. **Ошибки** возвращаются как `{'detail': '...'}`; тексты для пользователя — на русском.
3. **Время.** **Инвариант: `Event.time` в БД всегда хранится в локальном времени события
   (`UTC + timezone_offset`, смещение в минутах)** — независимо от клиента. Что принимает и отдаёт API,
   зависит от заголовка запроса `X-Time-Contract` (`tracker/time_contract.py`):
   - `X-Time-Contract: utc` — `time` в запросе и ответе в UTC; сервер переводит UTC ↔ локальное;
   - заголовка нет (legacy, старые клиенты) — `time` локальное, сервер **ничего не сдвигает**.
   Пересчёт — только через `time_to_stored` / `time_to_wire` (поверх `shift_time_by_minutes`), не вручную.
   Применённый режим сервер возвращает заголовком ответа `X-Time-Contract`. Порядок событий внутри дня —
   по моменту события (локальное время минус offset), а не по строке `time` из ответа.
   Legacy-ветка временная: **планируемая дата удаления — 31.01.2027** (подтверждается при выкатке R1,
   поведение после удаления — 426 «обновите приложение» либо трактовка как `utc` — решается по метрике).
   Любой новый эндпоинт, отдающий или принимающий время события, обязан поддерживать оба режима и иметь
   тесты на оба (см. `tests/test_time_contract.py`).
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
- **`send_event_notifications`** каждую минуту выбирает события с SQL-предфильтром по дате старта и
  `RecurrenceRule.until`; сбой одного события логируется и не останавливает остальных. Запись
  `EventNotificationLog` «занимается» до отправки (уникальный индекс), при ошибке отправки снимается.
  Окно срабатывания — `NOTIFICATION_LOOKBACK` (2 минуты).
- **Повторения.** Семантика — в docstring `tracker/recurrence.py`; любое её изменение сначала вносится в
  `tests/data/recurrence_vectors.json` (общий с приложением). `RecurrenceRule.until` — кэш последней даты:
  пересчитывается в сериализаторе при создании/правке правила и смене `start_date`. `/period/`: окно ≤ 400 дней,
  ≤ 10 000 вхождений, иначе 400; включён `GZipMiddleware`.
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

## Выкатка контракта времени (релиз R1)

Миграция `0018_event_time_to_local` меняет данные (`Event.time = time − timezone_offset`) и должна идти
**одним окном** с кодом режимов контракта (иначе строки сдвинутся дважды или останутся сдвинутыми):

1. Ночью, пока ни один клиент не шлёт `X-Time-Contract` (все клиенты legacy).
2. Остановить `web`, `celery` **и `celery-beat`** (старый код не должен ни писать события, ни слать пуши).
3. Развернуть новый образ и применить миграции (`python manage.py migrate`).
4. Запустить `web`, `celery`, `celery-beat`.
5. Проверка: событие, созданное без заголовка с `time=17:05`, `timezone_offset=180`, лежит в БД как 17:05, отдаётся
   как 17:05; с заголовком `utc` и `time=14:05` — лежит как 17:05, отдаётся как 14:05; пуш уходит в 17:05 МСК.
6. Откат: тот же порядок с прежним образом и `python manage.py migrate tracker 0017` (обратная миграция
   возвращает `local + offset` для всех строк).

Сборку приложения с заголовком выпускать **только после** этой миграции. Метрика: логгер `tracker.time_contract`
(INFO — каждый запрос к событиям с режимом и `X-App-Version`; WARNING `time_contract_missing` — запрос без заголовка от
версии не ниже `TIME_CONTRACT_MIN_APP_VERSION`; переменная окружения, пусто = предупреждения выключены).
