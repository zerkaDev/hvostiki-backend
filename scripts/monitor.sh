#!/bin/sh
# Мониторинг прод-сервера: пишет метрики в лог и предупреждает при выходе за пороги.
#
# Запуск по cron каждые 5 минут (см. crontab на сервере). Пороги можно переопределить
# переменными окружения. Алерт уходит в stdout/stderr файла лога, а если задан
# MONITOR_WEBHOOK — POST-ом в Telegram/Slack-вебхук (нужен curl).
set -eu

LOG="${MONITOR_LOG:-/var/log/hvostiki-monitor.log}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1/healthz}"
DISK_MAX="${DISK_MAX:-85}"
MEM_MIN_MB="${MEM_MIN_MB:-80}"
SWAP_MAX_MB="${SWAP_MAX_MB:-1024}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/hvostiki}"
COMPOSE_ARGS="${COMPOSE_ARGS:--f docker-compose.prod.yml -f docker-compose.server.yml}"

problems=""

# 1. Приложение отвечает?
health_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" || echo "000")
[ "$health_code" = "200" ] || problems="$problems healthz=$health_code"

# 2. Контейнеры запущены?
cd "$COMPOSE_DIR"
stopped=$(docker compose $COMPOSE_ARGS ps --format '{{.Service}}={{.State}}' 2>/dev/null | grep -v '=running' || true)
[ -z "$stopped" ] || problems="$problems не-запущены:[$stopped]"

# 3. Диск
disk=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${disk:-0}" -lt "$DISK_MAX" ] || problems="$problems диск=${disk}%"

# 4. Доступная память
mem_available=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
[ "${mem_available:-0}" -ge "$MEM_MIN_MB" ] || problems="$problems память=${mem_available}MB"

# 5. Swap (рост означает, что RAM не хватает)
swap_used=$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo)
[ "${swap_used:-0}" -le "$SWAP_MAX_MB" ] || problems="$problems swap=${swap_used}MB"

# 6. Медленные запросы за последние 5 минут (nginx main_ext, порог 1с)
slow=$(docker logs --since 5m hvostiki-nginx-1 2>&1 | grep -oE 'rt=[0-9.]+' | tr -d 'rt=' | awk '$1 >= 1.0' | wc -l | tr -d ' ')
slow=${slow:-0}
[ "$slow" -le "${SLOW_MAX:-20}" ] || problems="$problems медленных=${slow}"

# 7. Ошибки 5xx за последние 5 минут
errors=$(docker logs --since 5m hvostiki-nginx-1 2>&1 | grep -cE '" 5[0-9]{2} ' || true)
errors=${errors:-0}
[ "$errors" -le "${ERRORS_MAX:-5}" ] || problems="$problems 5xx=${errors}"

line="$(date -Is) healthz=$health_code disk=${disk}% mem=${mem_available}MB swap=${swap_used}MB slow=${slow} 5xx=${errors}"
echo "$line$( [ -n "$problems" ] && echo " | ПРОБЛЕМА:$problems" )" >> "$LOG"

# Обрезаем лог, чтобы не рос бесконечно
if [ "$(wc -l < "$LOG")" -gt 5000 ]; then
    tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

if [ -n "$problems" ] && [ -n "${MONITOR_WEBHOOK:-}" ]; then
    curl -s --max-time 10 -X POST "$MONITOR_WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "{\"text\":\"⚠️ hvostiki:${problems}\"}" >/dev/null || true
fi

# Ненулевой код возврата, чтобы cron-обёртка могла это заметить
[ -z "$problems" ]
