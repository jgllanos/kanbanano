FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv

WORKDIR /app

# Dependencies first, so editing app code doesn't redo this layer.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY app ./app

# Run as an unprivileged user. /data is created here, owned by that user, so
# the named volume mounted over it starts out writable by the app.
RUN useradd --system --no-create-home app \
    && mkdir /data \
    && chown app:app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    DB_PATH=/data/board.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
