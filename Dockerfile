# =========================
# Base layer: рантайм-зависимости, общие для dev и prod
# =========================
FROM python:3.13.7-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# libpq — рантайм psycopg2, curl — healthcheck в compose.
RUN apt-get update && apt-get install -y \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip poetry

COPY pyproject.toml poetry.lock ./

# =========================
# Dev stage
# =========================
FROM base AS dev

# Компиляторы нужны только для сборки psycopg2 из исходников.
RUN apt-get update && apt-get install -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN poetry config virtualenvs.create false \
    && poetry install --with dev --no-root

COPY . .

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# =========================
# Prod build layer
# =========================
# Зависимости ставятся в отдельный venv: в рантайм уедет только он, без poetry,
# компиляторов и dev-инструментов.
FROM base AS prod-build

RUN apt-get update && apt-get install -y build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
# Установка идёт в venv (poetry видит VIRTUAL_ENV даже при virtualenvs.create=false).
RUN python -m venv "$VIRTUAL_ENV" \
    && poetry install --only main --no-root

# =========================
# Prod stage
# =========================
FROM base AS prod

# Копируем готовый venv из build-слоя: poetry, компиляторы и исходники psycopg2
# остаются в промежуточном слое и в финальный образ не попадают.
COPY --from=prod-build /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY . .

# Права на код: процесс работает под app (uid 10001), поэтому все файлы должны быть
# читаемы. Иначе новый файл с локальными правами 600 положит приложение
# (PermissionError при импорте) — ровно так случилось с tracker/review_access.py.
RUN chmod -R a+rX /app

# Непривилегированный пользователь: веб и воркеры не должны работать от root.
# Статику и права на тома готовит entrypoint — тома монтируются уже после сборки.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/staticfiles /app/media /app/private_media \
    && chown -R app:app /app/staticfiles /app/media /app/private_media \
    && chmod +x /app/entrypoint.sh

# В рантайме poetry и pip не нужны: зависимости уже стоят в /opt/venv.
RUN pip uninstall -y poetry 2>/dev/null || true; \
    rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
          /usr/local/bin/poetry /usr/local/bin/poetry-plugin-export

ENV APP_USER=app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["web"]
