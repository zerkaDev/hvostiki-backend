# Прод: сервер v3278180 (vdsina)

Данные на 17.10.2026. Доступы (SSH-ключ, `.env`, пароль root) в git не хранятся —
они на сервере в `/opt/hvostiki` и локально в `secrets/`.

## Что это за сервер

| Параметр | Значение |
|---|---|
| Провайдер | vdsina, `v3278180.hosted-by-vdsina.ru` |
| ОС | Ubuntu 26.04 LTS |
| CPU / RAM | 1 vCPU / 956 МБ (+ 2 ГБ swap) |
| Диск | 10 ГБ (занято ~6 ГБ, свободно ~3.5 ГБ) |
| Трафик | 1 ТБ |
| IPv4 | 45.150.36.205 (7 ₽/день) |
| Прод-URL | **https://hvostiki-back.ru** (Let's Encrypt, до 2027-01-15) |

## Как устроен деплой

Образ **собирается не на сервере** (там 1 ядро и мало диска), а локально под нужную
архитектуру и загружается готовым:

```bash
# на локальной машине (Apple Silicon → linux/amd64)
docker buildx build --builder amd64builder --platform linux/amd64 \
  --target prod -t hvostiki-prod-amd64:latest --load .
docker save hvostiki-prod-amd64:latest | gzip -1 > /tmp/hvostiki-image.tar.gz
scp /tmp/hvostiki-image.tar.gz root@45.150.36.205:/tmp/
ssh root@45.150.36.205 'gunzip -c /tmp/hvostiki-image.tar.gz | docker load && rm /tmp/hvostiki-image.tar.gz'
```

`docker-compose.server.yml` — оверрайд для сервера: он подменяет `build` на готовый
образ, поэтому compose не пытается собирать на месте.

## Запуск и управление

Все команды — из `/opt/hvostiki` на сервере:

```bash
cd /opt/hvostiki
docker compose -f docker-compose.prod.yml -f docker-compose.server.yml up -d
docker compose -f docker-compose.prod.yml -f docker-compose.server.yml ps
docker compose -f docker-compose.prod.yml -f docker-compose.server.yml logs -f web
docker compose -f docker-compose.prod.yml -f docker-compose.server.yml down
```

Проверка живости: `curl http://45.150.36.205/healthz` → `{"status": "ok"}`.

## Что настроено на сервере

- **swap 2 ГБ** (`/swapfile`, в `/etc/fstab`) — на 956 МБ RAM без него не собрать и не пережить деплой;
- **Docker** с лимитом логов (10 МБ × 2 на контейнер) и `live-restore`, автозапуск включён;
- **ufw**: открыты только 22, 80, 443;
- **SSH**: только по ключу (`PasswordAuthentication no`). В `/root/.ssh/authorized_keys` два ключа:
  деплойный `dsh-deploy-hvostiki` (им пользуется агент через `secrets/deploy_key`) и личный ключ
  владельца `goat@MacBook-Pro-lil.local` — поэтому вход работает просто как `ssh root@45.150.36.205`.
  Доступ коллеге добавляется той же командой (публичный ключ в конец файла, права `600`/`700`),
  отзыв — удалением строки, перезапуск SSH не нужен;
- **бэкап**: `/opt/hvostiki/backup/backup.sh` по cron в 04:00, дамп БД + медиа, хранит 7 дней.
  Проверка: `zcat /opt/hvostiki/backup/db-*.sql.gz | grep -c "CREATE TABLE"` (должно быть > 0);
- **Firebase**: `secrets/firebase-key.json` смонтирован в `web` и `celery` только на чтение,
  права `10001:10001` — иначе процесс под `app` не может его прочитать.

## Тонкости, на которых уже спотыкались

- `celery` и `celery-beat` наследуют `HEALTHCHECK` образа (порт 8000), которого у них нет,
  поэтому в compose у них `healthcheck: disable: true`. «unhealthy» у воркера — ложная тревога.
- Домен и HTTPS не настроены: Let's Encrypt не выдаёт сертификаты на IP. После покупки домена
  нужны `A`-запись, `SERVER_NAME`/`DJANGO_ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` в `.env` и
  раскомментирование HTTPS-блока в `nginx/conf.d/default.conf.template`.
- Сертификат и `certbot` ещё не настроены: сейчас HTTP. Для приложения это работает, но
  трафик не шифруется — до релиза в сторы нужен домен + HTTPS.
- `/api/docs/` и `/api/schema/` открыты публично. Если это лишнее — закрыть на уровне nginx.

## Что сделать дальше

1. **Сменить пароль root** (он был в истории чата) — например `passwd`.
2. Купить домен, настроить `A`-запись и HTTPS по инструкции в README.
3. Создать суперпользователя админки: `docker compose ... exec web manage createsuperuser`.
4. Проверить push: отправить реальное событие с устройством и посмотреть логи `celery`.
5. Подключить внешний мониторинг `/healthz` (UptimeRobot и подобные).
6. Вынести медиа в S3-хранилище, когда кончится место на диске (сейчас 10 ГБ).
