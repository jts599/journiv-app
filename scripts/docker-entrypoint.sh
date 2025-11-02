#!/bin/sh
set -e

echo "Ensuring data directories exist..."
mkdir -p /data/media /data/logs

echo "Running database migrations in entrypoint script..."
alembic upgrade head || echo "Migration failed in entrypoint script, will fallback to create_all"

echo "Seeding initial data in entrypoint script..."
SKIP_DATA_SEEDING=false python -c "from app.core.database import seed_initial_data; seed_initial_data()" || echo "Data seeding failed, will retry in workers if needed"

echo "Starting Gunicorn..."
# Production uses 2 workers for optimal resource usage
# Increase -w flag if you need higher concurrency
exec gunicorn app.main:app -w 2 -k uvicorn.workers.UvicornWorker --worker-connections 1000 --max-requests 1000 --max-requests-jitter 100 -b 0.0.0.0:8000
