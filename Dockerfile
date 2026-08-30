# syntax=docker/dockerfile:1

# --- build frontend ---
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- build python env ---
FROM python:3.12-slim AS deps
# Pin the uv version; :latest makes builds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Lockfile first, without the project itself — this layer caches across source
# edits, so a code change doesn't reinstall every dependency.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime ---
FROM python:3.12-slim
WORKDIR /app
# uvicorn runs straight off the venv PATH, not via `uv run` — an extra process
# layer between Fly's kill_signal and uvicorn breaks graceful shutdown.
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
COPY --from=deps /app /app
COPY --from=web /web/dist ./static
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
