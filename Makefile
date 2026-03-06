# ====== CONFIG ======
DC = docker compose
FILE = docker-compose.dev.yml
SERVICE = web

# ====== BASE ======
.PHONY: help up down restart ps logs build

help:
	@echo "Available commands:"
	@echo "  make up        - Start containers in background"
	@echo "  make down      - Stop and remove containers"
	@echo "  make restart   - Restart containers"
	@echo "  make ps        - List running containers"
	@echo "  make logs      - Show and follow logs for web service"
	@echo "  make build     - Rebuild images"
	@echo ""
	@echo "Django commands:"
	@echo "  make shell     - Open shell in web container"
	@echo "  make ds        - Open Django shell (shell_plus or regular)"
	@echo "  make migrate   - Run migrations"
	@echo "  make mm        - Make migrations AND migrate"
	@echo "  make su        - Create superuser"
	@echo "  make static    - Collect static files"
	@echo ""
	@echo "Tests & Linting:"
	@echo "  make test      - Run all tests (usage: make test args=\"-v path/to/test\")"
	@echo "  make lint      - Check code style (black)"
	@echo "  make format    - Autoformat code (black)"
	@echo ""
	@echo "Utils:"
	@echo "  make clean     - Stop and remove volumes/orphans"
	@echo "  make reset-db  - Flush database (CAUTION!)"

up:
	$(DC) -f $(FILE) up -d

down:
	$(DC) -f $(FILE) down

restart:
	$(DC) -f $(FILE) down
	$(DC) -f $(FILE) up -d

ps:
	$(DC) -f $(FILE) ps

logs:
	$(DC) -f $(FILE) logs -f $(SERVICE)

build:
	$(DC) -f $(FILE) build

# ====== DJANGO ======
.PHONY: shell ds migrate mm su static

shell:
	$(DC) -f $(FILE) exec $(SERVICE) sh

ds:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py shell_plus || \
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py shell

migrate:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py migrate

mm:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py makemigrations
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py migrate

su:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py createsuperuser

static:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py collectstatic --noinput

# ====== TESTS & LINTING ======
.PHONY: test lint format

test:
	$(DC) -f $(FILE) exec $(SERVICE) pytest $(args)

lint:
	$(DC) -f $(FILE) exec $(SERVICE) black --check .

format:
	$(DC) -f $(FILE) exec $(SERVICE) black .

# ====== UTILS ======
.PHONY: clean reset-db

clean:
	$(DC) -f $(FILE) down -v --remove-orphans

reset-db:
	$(DC) -f $(FILE) exec $(SERVICE) python manage.py flush
