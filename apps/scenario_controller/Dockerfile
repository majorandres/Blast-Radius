FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /repo/apps/scenario_controller

COPY packages/contracts /repo/packages/contracts
COPY apps/scenario_controller/pyproject.toml apps/scenario_controller/uv.lock /repo/apps/scenario_controller/
RUN uv sync --frozen --no-install-project

COPY apps/scenario_controller /repo/apps/scenario_controller
RUN uv sync --frozen

ENV PATH="/repo/apps/scenario_controller/.venv/bin:$PATH"
