FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY cmd ./cmd
COPY internal ./internal
COPY migrations ./migrations


FROM python:3.12-slim

# git keeps the clone of the code; ffmpeg takes frames out of the videos clients send.
# Packages come from the Yandex mirror.
RUN sed -i 's#http://deb.debian.org/#http://mirror.yandex.ru/#' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

USER nobody

CMD ["python", "cmd/bot/main.py"]
