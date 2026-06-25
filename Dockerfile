FROM python:3.11-slim-bookworm AS runtime

ARG PIP_INDEX_URL
ARG APT_MIRROR_URL
ARG NPM_CONFIG_REGISTRY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    ZLAGENT_HOST=0.0.0.0 \
    ZLAGENT_PORT=8020 \
    ZLAGENT_DATA_DIR=/app/data \
    ZLAGENT_CONFIG_DIR=/app/config \
    ZLAGENT_WORKSPACE_DIR=/app/workspace \
    ZLAGENT_PACKAGES_DIR=/app/.packages \
    ZLAGENT_FASTEMBED_CACHE_DIR=/app/data/fastembed \
    NPM_CONFIG_PREFIX=/app/.packages/npm \
    NPM_CONFIG_IGNORE_SCRIPTS=true \
    NPM_CONFIG_CACHE=/app/.packages/npm-cache \
    UV_CACHE_DIR=/app/.packages/uv-cache \
    UV_TOOL_DIR=/app/.packages/uv/tools \
    UV_TOOL_BIN_DIR=/app/.packages/pip/bin \
    UV_PYTHON_INSTALL_DIR=/app/.packages/uv/python \
    PIP_USER=1 \
    PIP_CACHE_DIR=/app/.packages/pip-cache \
    PYTHONUSERBASE=/app/.packages/pip \
    PATH=/app/.packages/npm/bin:/app/.packages/pip/bin:/app/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin

WORKDIR /app

# Install Node.js LTS so the mcp-discovery skill can shell out to
# ``npx -y mcporter ...`` and the broader npm-distributed MCP server
# ecosystem. ffmpeg is included for media-oriented MCP servers, and
# /app/Downloads exists for servers that validate a download directory
# at startup. This layer sits before app code to keep rebuilds fast.
#
# APT_MIRROR_URL build-arg follows the PIP_INDEX_URL pattern: pass it
# from a network where the default Debian mirrors are slow or
# unreachable (notably mainland China). Maps both deb.debian.org and
# security.debian.org to the supplied base URL. Examples:
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.tuna.tsinghua.edu.cn
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.aliyun.com
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.ustc.edu.cn
# Bookworm uses the DEB822 /etc/apt/sources.list.d/debian.sources
# format; older /etc/apt/sources.list is patched too as a safety net.
RUN set -eux; \
    if [ -n "${APT_MIRROR_URL:-}" ]; then \
        MIRROR="${APT_MIRROR_URL%/}"; \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then \
                sed -i \
                    -e "s|https\\?://deb.debian.org|$MIRROR|g" \
                    -e "s|https\\?://security.debian.org|$MIRROR|g" \
                    "$f"; \
            fi; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg ffmpeg passwd; \
    if command -v python3 >/dev/null 2>&1 && [ ! -x /usr/local/bin/python3.11 ]; then \
        ln -sf "$(command -v python3)" /usr/local/bin/python3.11; \
    fi; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --retries 5 --timeout 120 -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
        pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt; \
    fi

# Pre-install ``uv`` and ``uvx`` so Python-flavour MCP servers can be
# managed without an extra bootstrap roundtrip.
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --retries 5 --timeout 120 -i "$PIP_INDEX_URL" uv; \
    else \
        pip install --no-cache-dir --retries 5 --timeout 120 uv; \
    fi

COPY frontend ./frontend
RUN if [ -n "${NPM_CONFIG_REGISTRY:-}" ]; then \
        npm config set registry "$NPM_CONFIG_REGISTRY"; \
    fi; \
    if [ -f frontend/package.json ]; then \
        cd frontend \
        && NPM_CONFIG_IGNORE_SCRIPTS=false npm install --no-audit --no-fund \
        && npm run build \
        && rm -rf node_modules; \
    fi

RUN groupadd --gid 10001 zlagent \
    && useradd --uid 10001 --gid 10001 --home /app \
        --no-create-home --shell /usr/sbin/nologin zlagent \
    && mkdir -p /app/data /app/config /app/workspace /app/Downloads \
        /app/.packages/npm /app/.packages/npm-cache \
        /app/.packages/pip /app/.packages/pip/bin /app/.packages/pip-cache \
        /app/.packages/uv-cache /app/.packages/uv/tools /app/.packages/uv/python \
    && chown -R zlagent:zlagent /app/data /app/config /app/workspace /app/Downloads /app/.packages

# Keep build layers lean. MCP servers can be installed at runtime into
# the persisted /app/.packages volume, avoiding a large build layer.
USER root

COPY backend ./backend
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY workspace ./workspace_seed
COPY docker-entrypoint.py ./docker-entrypoint.py
RUN chown -R zlagent:zlagent /app/backend /app/alembic /app/alembic.ini /app/frontend /app/workspace_seed /app/docker-entrypoint.py \
    && chmod -R u=rwX,go=rX /app/backend /app/alembic /app/frontend /app/workspace_seed \
    && chmod 0444 /app/alembic.ini \
    && chmod 0555 /app/docker-entrypoint.py

USER zlagent

EXPOSE 8020

VOLUME ["/app/data", "/app/config", "/app/workspace", "/app/.packages"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"ZLAGENT_PORT\",\"8020\")}/api/health', timeout=5).read()" || exit 1

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
