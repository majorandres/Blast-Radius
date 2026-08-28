FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1     UV_LINK_MODE=copy     PYTHONUNBUFFERED=1

WORKDIR /repo/apps/ordering_app

# Path dependency: the repo layout is mirrored so ../../packages/contracts resolves.
COPY packages/contracts /repo/packages/contracts
COPY apps/ordering_app/pyproject.toml apps/ordering_app/uv.lock /repo/apps/ordering_app/
RUN uv sync --frozen --no-install-project

COPY apps/ordering_app /repo/apps/ordering_app
RUN uv sync --frozen

ENV PATH="/repo/apps/ordering_app/.venv/bin:$PATH"
