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
                 EventNotificationLog, FCMDevice, NotificationSettings, Notification, Feedback
  serializers.py DRF-сериализаторы
  schemas.py     drf-spectacular схемы (документация эндпоинтов)
  views.py       APIView / ModelViewSet'ы
  urls.py        маршруты (auth/*, profile/*, feedback/, breeds/, pets/, event_schedule/, devices/register/, devices/unregister/, notifications/*)
  tasks.py       Celery-задачи (send_confirmation_code, send_event_notifications,
                 cleanup_old_notifications, flush_expired_tokens)
  services/      firebase_service.py (FCM), ucalles_service.py (звонки-коды),
                 avatar.py (обработка фото профиля), account_deletion.py (код и удаление аккаунта),
                 feedback_notifier.py (канал доставки обращений, заглушка LogNotifier)
  notification_categories.py  категории уведомлений ↔ типы событий
  recurrence.py  движок повторений (чистые функции): якорь, clamp, -1, yearly, until, normalize_rule
  utils.py       generate_occurrences (тонкая обёртка над движком), shift_time_by_minutes, normalize_phone
  event_time.py  время события: UTC на проводе ↔ локальное в БД (time_to_stored / time_to_wire)
  backends.py    PhoneBackend (вход в Django Admin по телефону)
  tests/         pytest: conftest.py + test_auth/test_pets/test_events/test_notifications/test_profile/test_profile_avatar/test_account_deletion/test_feedback/test_notification_settings/test_devices/test_event_time/test_recurrence_* (vectors/api/slots)
  tests/data/recurrence_vectors.json  общие векторы повторений (те же, что в мобильном приложении)
  migrations/    0001..0025
```

## Соглашения проекта

1. **Схемы API обязательны.** Для каждого нового вью/экшена добавляй `@extend_schema` в `tracker/schemas.py`
   (теги и описания — на русском). Проверка: `python manage.py spectacular --file /tmp/schema.yml`.
2. **Ошибки** возвращаются как `{'detail': '...'}`; тексты для пользователя — на русском.
3. **Время.** **Инвариант: `Event.time` и слоты `RecurrenceRule.times` в БД всегда хранятся в локальном
   времени события (`UTC + timezone_offset`, смещение в минутах).** API принимает и отдаёт время **в UTC**
   (`time`, `recurrence.times`, `time` в `mark_done`/`mark_undone`); дата вхождения (`start_date`) — локальная.
   Пересчёт — только через `time_to_stored` / `time_to_wire` (`tracker/event_time.py`, поверх
   `shift_time_by_minutes`), не вручную. Порядок событий внутри дня — по моменту события (локальное время
   минус offset), а не по строке `time` из ответа. Новый эндпоинт, отдающий или принимающий время события,
   обязан конвертировать так же и иметь тесты (см. `tests/test_event_time.py`).
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
   контракта с мобильным клиентом — не удалять. Исключение: в списках `/event_schedule/period/` и
   `/pets/{id}/upcoming/` у событий `pet_obj` нет (клиент берёт питомца по `pet` из своего списка).
8. **Секреты.** `.env` и `firebase-key.json` в `.gitignore` — никогда не коммитить. Push не работает, если
   файла ключа нет: по умолчанию `firebase-key.json` в корне, путь меняется переменной `FIREBASE_CREDENTIALS_FILE`.

## Вход (контракт)

- `POST /auth/verify-code/` `{phone_number, code}` → `refresh, access, access_expires, refresh_expires` плюс
  `is_new_user` (bool) и `user_id` (строка, тот же `id`, что в `GET /profile/`). Поля нужны мобильной аналитике;
  `/auth/token/refresh/` их не возвращает.
- Пользователь создаётся уже в `POST /auth/send-code/` (`get_or_create`), поэтому `is_new_user` — это **первое
  успешное подтверждение номера** (`is_verified` был `False`), а не «запись создана в этом запросе».
  Признак ставится атомарным условным UPDATE (`filter(pk, is_verified=False).update(...)`): при гонке или двойной
  отправке `true` получит ровно один запрос. Неверный код признак не расходует. Повторный вход с любого
  устройства — `false`. Удаление аккаунта полное (каскад), поэтому повторная регистрация того же номера создаёт
  новую запись и снова даёт `true` (с новым `user_id`).
- Старые версии приложения новые поля игнорируют, миграции не требуются.

## Раздел «Профиль» (контракт)

- `GET /profile/` → `id, phone_number, name, avatar (абсолютный URL | null), notification_settings, is_verified, created_at`.
- `PATCH /profile/` (JSON или multipart): `name` (≤50, пустая строка очищает), `avatar` (JPG/PNG/HEIC ≤10 МБ → JPEG ≤1024 px).
  **Номер телефона изменить нельзя** (поле игнорируется), PUT и DELETE на `/profile/` не поддерживаются.
- `DELETE /profile/avatar/` — 204, идемпотентно.
- `GET/PATCH /profile/notification-settings/` — карта `walks|feeding|medications|vaccinations|vet_visits → bool`;
  хранятся только отключённые категории (`NotificationSettings.disabled_categories`), по умолчанию включено всё.
  Соответствие типам событий — `tracker/notification_categories.py`; типы вне соответствия (grooming, bathing,
  nailTrimming, custom) уведомляют всегда. Фильтр — в `_notify_slot` **до** занятия `EventNotificationLog`.
- Удаление аккаунта: `POST /profile/delete/send-code/` (звонок, 429 чаще раза в минуту) → `POST /profile/delete/` `{code}` → 204.
  Код хранится в кэше (5 мин, 5 попыток), отдельно от кода входа, в логи не пишется. Удаление атомарно:
  refresh-токены в blacklist, каскадное удаление, файлы (аватар, фото питомцев) удаляются через storage `on_commit`.
- `POST /feedback/` — `topic` problem|idea|question, `message` 1–2000, `screenshot` ≤10 МБ, данные устройства; лимит 5/час
  (`FEEDBACK_THROTTLE_RATE`). Доставка — Celery `deliver_feedback` через `FEEDBACK_NOTIFIER` (по умолчанию
  `LogNotifier`-заглушка; для Telegram-бота — новый класс с `send(feedback)` и смена настройки).
- Журнал приложения в обращении: необязательное multipart-поле `logs` (gzip, `application/gzip`, UTF-8 текст внутри)
  в `POST /feedback/`; только на запись (в ответе нет). Лимиты: файл ≤ 2 МБ и ≤ 10 МБ после распаковки (потоковая
  проверка, содержимое не разбирается) — иначе 413; не gzip или оборван — 400. Хранится в **приватном** хранилище
  (`STORAGES['private']`, `PRIVATE_MEDIA_ROOT`, вне `MEDIA_ROOT`, без URL, имя файла генерирует сервер,
  `tracker/storage.py`). Скачать может только staff с правом просмотра (Django Admin, ссылка в обращении; каждое
  скачивание пишется в лог). Удаляется задачей `delete_expired_feedback_logs` (beat, раз в сутки) через
  `FEEDBACK_LOGS_RETENTION_DAYS` (30) дней — обращение остаётся; при удалении аккаунта и при удалении обращения из
  админки — сразу. Содержимое файла нигде не логируется. В prod том `private_media` монтируется в `web` и `celery`;
  лимит тела запроса на уровне reverse proxy (если появится) должен быть не меньше ~2,5 МБ.
- Админка обращений: в списке колонка **«Обратная связь»** — текст обращения (ссылка на карточку, длинный
  обрезается) и превью скриншота (`<img>` 48 px, клик открывает оригинал; сам файл отдаётся по `/media/`, поэтому
  превью видно только при `SERVE_MEDIA=1` или DEBUG, в prod — если nginx раздаёт `/media/`). Ссылки на журнал в
  списке нет, чтобы не дублировать подпись в каждой строке: скачивание — в карточке, где поля сгруппированы в
  «Обратная связь» (тема, текст, скриншот с превью 600 px, журнал) и «Техническая информация».
- Медиа: `MEDIA_ROOT`/`MEDIA_STORAGE_BACKEND` из env, в prod — том `media`; `/media/` отдаёт Django при DEBUG или `SERVE_MEDIA=1`.
  Файлы удалять только через storage API (`field.storage.delete`), не через `os`.

## Push-уведомления (FCM)

- `POST /devices/register/` `{fcm_token, platform?}` (`platform` — `ios`|`android`, без него ранее сохранённое значение
  не меняется); токен уже существующий у другого пользователя перепривязывается к текущему.
- `POST /devices/unregister/` `{fcm_token}` — отвязка при выходе из аккаунта (вызывать до `/auth/logout/`, пока JWT
  действует). Идемпотентно, чужой токен не удаляется.
- `FirebaseService.send_push_notification`: `android.priority=high`, `apns-priority=10` и звук; токен, который FCM признал
  недействительным (`UnregisteredError`, `SenderIdMismatchError`), удаляется из БД; прочие ошибки пробрасываются,
  чтобы `_notify_slot` снял запись `EventNotificationLog` и повторил отправку в окне `NOTIFICATION_LOOKBACK`.
  Сбой одного устройства не мешает остальным; запись снимается, только если не дошло ни до одного.
- Данные пуша (`data`, только строки): `event_id`, `type` (`standard|reminder|final`), `date` (дата вхождения, ISO),
  `time` (`HH:MM`, только при нескольких слотах в день). Мобильное приложение использует их при нажатии на уведомление.
- `firebase-admin` в зависимостях обязателен: без него `HAS_FIREBASE=False` и пуши молча не уходят.

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
  ≤ 10 000 вхождений (считаются слоты), иначе 400; включён `GZipMiddleware`.
- **Несколько времён в день.** `RecurrenceRule.times` (только daily, ≥2 значений, локальное время, `HH:MM`);
  при одном слоте поле пустое, время в `Event.time`; `Event.time` = первый слот. В `/period/` — запись на слот
  (`time` слота, `done` по слоту). `EventCompletion`/`EventNotificationLog.occurrence_time` (NULL — «как раньше»,
  в многослотовом режиме NULL-отметка относится к первому слоту); `mark_done`/`mark_undone` при >1 слоте требуют
  `time`. Уведомления — по каждому слоту, в данных пуша `time`.
- **`token_blacklist`** растёт: чистка висит на задаче `flush_expired_tokens` в beat, а `celery-beat`
  обязателен и в проде (`docker-compose.prod.yml`).
- **Уведомления** рассылаются только тем устройствам, что зарегистрированы через `POST /devices/register/`;
  без ключа Firebase отправка молча пропускается (в логах warning).
- Админка: вход staff-пользователя без пароля с пустым паролем разрешён только при `DEBUG=1`.

## Правила работы для агентов

- Минимальные диффы в стиле окружающего кода; не переформатировать и не «улучшать» файлы, не относящиеся к задаче.
- Пользовательские тексты — на русском, как в существующем коде.
- Миграции не править задним числом — только добавлять новые (`make mm`).
- Новые зависимости — в `pyproject.toml` (Poetry) + обновлённый `poetry.lock` (`poetry lock`, версии
  остальных пакетов при этом не поднимаются).
- Не коммитить `.env`, `firebase-key.json`, `celerybeat-schedule`, `media/`.
- Перед завершением задачи: `pytest` + `manage.py check` + `makemigrations --check --dry-run`.

## Выкатка миграции времени (0018)

Миграция `0018_event_time_to_local` меняет данные (`Event.time = time − timezone_offset`: раньше приложение
присылало локальное время как UTC, и в БД оно лежало сдвинутым). Её нужно применить **одним окном** с деплоем
кода: остановить `web`, `celery` и `celery-beat`, развернуть образ, `python manage.py migrate`, запустить
сервисы. Проверка: событие `time=14:05`, `timezone_offset=180` лежит в БД как 17:05, отдаётся как 14:05, пуш
уходит в 17:05 МСК. Откат: прежний образ и `python manage.py migrate tracker 0017`.

## Центр уведомлений (inbox)

- Модель `Notification`: история пушей для экрана «Уведомления». Запись для напоминания о событии создаётся в
  `_notify_slot` вместе с `EventNotificationLog` (OneToOne `log`): если отправка не удалась и лог снимается,
  запись уходит по каскаду, повтор создаст её заново. Создаётся и без устройств. Отключённые категории ничего не
  создают. `kind=announcement` — общие объявления без питомца/события (пока только из админки).
- Текст: `title` = название события, `body` = описание, а без него `«{питомец}: пора выполнить»` (накануне —
  `«{питомец}: напоминание на завтра»`). Тот же текст идёт в пуш. Удаление питомца/события/пользователя каскадно
  удаляет уведомления; история старше `NOTIFICATION_RETENTION_DAYS` (180) чистится `cleanup_old_notifications`.
- `data` пуша: `notification_id`, `event_id` (UUID), `pet_id`, `event_type`, `type`, `date`, `time` (UTC, только
  для событий с несколькими временами в день). `occurrence_time` в БД локальное, в API — UTC (`time_to_wire`).
- API: `GET /notifications/` (`limit` 1–100, `cursor`, `unread=true`; ответ `results`, `next_cursor`,
  `unread_count`), `GET /notifications/unread-count/`, `POST /notifications/{id}/read/` (идемпотентно, чужое — 404),
  `POST /notifications/read-all/`. Пагинация — keyset по `(created_at, id)`; глобальной пагинации DRF в проекте нет.
