FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1     UV_LINK_MODE=copy     PYTHONUNBUFFERED=1

WORKDIR /repo/apps/promo_provider

# Path dependency: the repo layout is mirrored so ../../packages/contracts resolves.
COPY packages/contracts /repo/packages/contracts
COPY apps/promo_provider/pyproject.toml apps/promo_provider/uv.lock /repo/apps/promo_provider/
RUN uv sync --frozen --no-install-project

COPY apps/promo_provider /repo/apps/promo_provider
RUN uv sync --frozen

ENV PATH="/repo/apps/promo_provider/.venv/bin:$PATH"
