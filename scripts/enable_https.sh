#!/bin/sh
# Включает HTTPS на сервере после выпуска сертификата Let's Encrypt.
#
# Что делает:
#   1. Проверяет, что сертификат для домена из .env существует.
#   2. Ставит nginx-конфиг с 443 и редиректом с 80 (default-https.conf.template).
#   3. Переключает SECURE_SSL_REDIRECT/COOKIE_SECURE в 1.
#   4. Перезапускает nginx и web, проверяет https и редирект.
#
# Запуск на сервере из /opt/hvostiki: sh scripts/enable_https.sh
set -eu

cd /opt/hvostiki
COMPOSE="docker compose -f docker-compose.prod.yml -f docker-compose.server.yml"

# shellcheck disable=SC1091
. ./.env
DOMAIN="${SERVER_NAME:?SERVER_NAME не задан в .env}"

CERT="/opt/hvostiki/certbot/conf/live/$DOMAIN/fullchain.pem"
if [ ! -f "$CERT" ]; then
    echo "Сертификат не найден: $CERT" >&2
    echo "Сначала выпустите его:" >&2
    echo "  $COMPOSE --profile tools run --rm certbot certonly --webroot -w /var/www/certbot \\" >&2
    echo "    -d $DOMAIN -d www.$DOMAIN --email <ваш email> --agree-tos --no-eff-email --non-interactive" >&2
    exit 1
fi

echo "1/4 Конфиг nginx: HTTPS для $DOMAIN"
cp nginx/conf.d/default-https.conf.template nginx/conf.d/default.conf.template

echo "2/4 Переключаем флаги безопасности в .env"
sed -i 's/^SECURE_SSL_REDIRECT=.*/SECURE_SSL_REDIRECT=1/' .env
sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' .env
grep -E '^(SECURE_SSL_REDIRECT|COOKIE_SECURE)=' .env

echo "3/4 Перезапуск nginx и web"
$COMPOSE up -d --force-recreate nginx web >/dev/null
sleep 15

echo "4/4 Проверки"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/healthz")
echo "  https://$DOMAIN/healthz -> $code"
redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 "http://$DOMAIN/healthz")
echo "  http://$DOMAIN/healthz -> $redirect"
echo "  сертификат:"
echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null \
    | openssl x509 -noout -subject -dates -issuer | sed 's/^/    /'

[ "$code" = "200" ] || { echo "HTTPS не отвечает 200 — проверьте логи nginx" >&2; exit 1; }
echo "Готово."
