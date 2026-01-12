# =========================
# Base layer
# Запускаем без poetry run тк не создаем poetry venv - плохо работает в контейнерах
# =========================
FROM python:3.13.7-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip poetry

COPY pyproject.toml poetry.lock ./

# =========================
# Dev stage
# =========================
FROM base AS dev

RUN poetry config virtualenvs.create false \
    && poetry install --extras dev --no-root

COPY . .

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# =========================
# Prod stage
# =========================
FROM base AS prod

RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-root

COPY . .

RUN python manage.py collectstatic --noinput

CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000"]
