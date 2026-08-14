# Образ бота: Python 3.14, зависимости только через uv, без pip в систему.
FROM python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Слой зависимостей: lock + pyproject, без исходников проекта.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY pyproject.toml uv.lock alembic.ini ./
COPY src ./src
COPY alembic ./alembic

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data

# Старт от root: container.py делает chown /data и setuid 1000.
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "pubmed_bot.container"]
