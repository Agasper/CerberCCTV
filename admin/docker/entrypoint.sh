#!/bin/sh
set -e

# Дождаться БД (в dev-компоузе Postgres может стартовать позже),
# накатить миграции и запустить приложение.
python -m cerber_admin.wait_db
alembic -c /app/admin/alembic.ini upgrade head
exec uvicorn cerber_admin.main:app --host 0.0.0.0 --port "${PORT:-8080}" \
    --proxy-headers --forwarded-allow-ips '*'
