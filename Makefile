# ====== CONFIG ======
DC = docker compose
FILE = docker-compose.dev.yml
SERVICE = web

# ====== BASE ======
.PHONY: help up down restart ps logs build

help:
	@echo "Available commands:"
	@echo "  make up        - start containers"
	@echo "  make down      - stop containers"
	@echo "  make restart   - restart containers"
	@echo "  make ps        - show containers"
	@echo "  make logs      - logs for web"
	@echo "  make build     - rebuild images"

up:
	$(DC) -f $(FILE) up -d

down:
	$(DC) -f $(FILE) down

restart:
	make down
	make up

ps:
	$(DC) -f $(FILE) ps

logs:
	$(DC) -f $(FILE) logs -f $(SERVICE)

build:
	$(DC) -f $(FILE) build

# ====== DJANGO ======
.PHONY: shell django-shell migrate makemigrations createsuperuser collectstatic

shell:
	$(DC) -f $(FILE) exec $(SERVICE) sh

django-shell:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py shell_plus || \
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py shell

migrate:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py migrate

makemigrations:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py makemigrations

createsuperuser:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py createsuperuser

collectstatic:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py collectstatic --noinput

# ====== TESTS ======
.PHONY: test test-one

test:
	$(DC) -f $(FILE) exec $(SERVICE) pytest

test-one:
	@read -p "Test path: " path; \
	$(DC) -f $(FILE) exec $(SERVICE) pytest $$path

# ====== UTILS ======
.PHONY: clean reset-db

clean:
	$(DC) -f $(FILE) down -v --remove-orphans

reset-db:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py flush
