FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /repo/migrations

COPY migrations/pyproject.toml migrations/uv.lock /repo/migrations/
RUN uv sync --frozen --no-install-project

COPY migrations /repo/migrations
RUN uv sync --frozen

ENV PATH="/repo/migrations/.venv/bin:$PATH"
ENTRYPOINT ["/repo/migrations/entrypoint.sh"]
