# One image per service, each with only its own dependency group:
#   docker build --build-arg GROUP=api|mcp|agent .
# docker-compose.yml builds all three and sets each one's command.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/

ARG GROUP
ENV UV_PYTHON_DOWNLOADS=0 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, in their own layer: rebuilt only when the lock file changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-default-groups --group ${GROUP} --no-install-project --no-editable

# Then the project itself, installed into the venv (non-editable, so the source isn't needed).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --group ${GROUP} --no-editable


FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
COPY --from=builder --chown=app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
WORKDIR /home/app
