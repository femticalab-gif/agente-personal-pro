# syntax=docker/dockerfile:1.7
# Imagen del agente: slim, sin root, sin compiladores en runtime.

FROM python:3.12.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
# Dependencias primero (capa cacheable), con versiones exactas del lockfile.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.13-slim-trixie
RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --home-dir /run/claude --shell /usr/sbin/nologin agent \
    && mkdir -p /data /run/claude /app \
    && chown agent:agent /data /run/claude \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
COPY config/config.example.yaml /app/config.example.yaml
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MARC_CONFIG=/app/config.yaml \
    MARC_CLAUDE_CONFIG_DIR=/run/claude \
    HOME=/run/claude \
    PTB_TIMEDELTA=1
USER agent
WORKDIR /data
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["marc-agent", "healthcheck"]
ENTRYPOINT ["marc-agent"]
CMD ["run"]
