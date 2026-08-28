FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /repo/apps/observability_service

COPY packages/contracts /repo/packages/contracts
COPY apps/observability_service/pyproject.toml apps/observability_service/uv.lock /repo/apps/observability_service/
RUN uv sync --frozen --no-install-project

COPY apps/observability_service /repo/apps/observability_service
RUN uv sync --frozen

ENV PATH="/repo/apps/observability_service/.venv/bin:$PATH"
