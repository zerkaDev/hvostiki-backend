#!/bin/sh
# Точка входа прод-контейнеров: подготовка файлов, миграции и запуск процесса.
#
# Коллект статики и миграции живут здесь, а не в docker build: на сборке нет .env
# (SECRET_KEY из окружения), а миграции должны применяться к живой БД при старте.
set -e

RUN_MIGRATIONS="${RUN_MIGRATIONS:-1}"
RUN_COLLECTSTATIC="${RUN_COLLECTSTATIC:-1}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-3}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-60}"
# На 1 vCPU пул prefork по умолчанию (число ядер + 1) даёт лишние процессы и память.
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"
APP_USER="${APP_USER:-app}"

log() {
    echo "[entrypoint] $*"
}

# Тома монтируются от root, а процессы работают под непривилегированным app.
prepare_dirs() {
    for dir in /app/staticfiles /app/media /app/private_media /app/celerybeat; do
        mkdir -p "$dir"
        chown -R "$APP_USER:$APP_USER" "$dir"
    done
}

# Ждём БД: при первом старте стек поднимается параллельно, а migrate падает на «connection refused».
wait_for_db() {
    attempts="${DB_WAIT_ATTEMPTS:-60}"
    while [ "$attempts" -gt 0 ]; do
        if python - <<'PY'
import os, sys
import psycopg2
try:
    psycopg2.connect(
        dbname=os.getenv('POSTGRES_DB'),
        user=os.getenv('POSTGRES_USER'),
        password=os.getenv('POSTGRES_PASSWORD'),
        host=os.getenv('POSTGRES_HOST', 'postgres'),
        port=os.getenv('POSTGRES_PORT', '5432'),
        connect_timeout=3,
    ).close()
except Exception as exc:  # БД ещё не готова
    print(exc)
    sys.exit(1)
PY
        then
            return 0
        fi
        attempts=$((attempts - 1))
        sleep 2
    done
    log "БД не ответила за отведённое время — выходим"
    return 1
}

# setpriv сбрасывает права до app, но init-группы нужны, чтобы работал доступ к файлам.
run_as_app() {
    exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups "$@"
}

prepare_dirs

if [ "$RUN_MIGRATIONS" = "1" ]; then
    wait_for_db
    log "Применяем миграции"
    python manage.py migrate --noinput
fi

if [ "$RUN_COLLECTSTATIC" = "1" ]; then
    log "Собираем статику"
    python manage.py collectstatic --noinput
fi

case "$1" in
    web)
        log "Запускаем gunicorn: $GUNICORN_WORKERS воркеров, таймаут ${GUNICORN_TIMEOUT}s"
        run_as_app gunicorn config.wsgi:application \
            --bind 0.0.0.0:8000 \
            --workers "$GUNICORN_WORKERS" \
            --timeout "$GUNICORN_TIMEOUT" \
            --access-logfile - \
            --error-logfile -
        ;;
    worker)
        log "Запускаем celery worker (concurrency=$CELERY_CONCURRENCY)"
        run_as_app celery -A config worker -l INFO --concurrency "$CELERY_CONCURRENCY"
        ;;
    beat)
        log "Запускаем celery beat"
        # Файл расписания — в томе /app/celerybeat: рабочий каталог /app принадлежит root,
        # и без явного пути beat падает с PermissionError.
        run_as_app celery -A config beat -l INFO \
            --schedule /app/celerybeat/celerybeat-schedule \
            --pidfile /app/celerybeat/celerybeat.pid
        ;;
    manage)
        shift
        run_as_app python manage.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
