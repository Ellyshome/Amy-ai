#!/bin/bash
set -e

HASH_FILE="$VIRTUAL_ENV/.deps-hash"

if [ -f /app/pyproject.toml ] && [ -f /app/uv.lock ]; then
    CURRENT_HASH=$(cat /app/pyproject.toml /app/uv.lock | md5sum | cut -d' ' -f1)
else
    CURRENT_HASH=""
fi

STORED_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")

if [ "$CURRENT_HASH" != "$STORED_HASH" ] || [ ! -d "$VIRTUAL_ENV/bin" ]; then
    if [ ! -f /app/pyproject.toml ] || [ ! -f /app/uv.lock ]; then
        echo "[entrypoint] Dependency files missing, skipping install."
    else
        echo "[entrypoint] Dependencies changed or venv missing, installing..."
        cd /app
        uv sync --frozen --no-install-project --active
        echo "$CURRENT_HASH" > "$HASH_FILE"
        echo "[entrypoint] Done."
    fi
else
    echo "[entrypoint] Dependencies unchanged, skipping install."
fi

exec "$@"
