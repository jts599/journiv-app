#!/bin/bash

# Database Migration Script
set -e

echo "🗄️ Running database migrations..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found. Please run ./scripts/setup.sh first."
    exit 1
fi

# Determine compose file based on environment
if [[ "${ENVIRONMENT:-development}" == "production" ]]; then
    COMPOSE_FILE="docker-compose.yml"
else
    COMPOSE_FILE="docker-compose.dev.yml"
fi

# Check if compose file exists
if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "❌ Compose file not found: $COMPOSE_FILE"
    exit 1
fi

echo "📋 Using compose file: $COMPOSE_FILE"

# Check if services are running
if ! docker-compose -f "$COMPOSE_FILE" ps | grep -q "Up"; then
    echo "🐳 Starting services..."
    docker-compose -f "$COMPOSE_FILE" up -d
    echo "⏳ Waiting for services to be ready..."
    sleep 10
fi

# Run migrations
echo "📝 Applying database migrations..."
if docker-compose -f "$COMPOSE_FILE" exec app alembic upgrade head; then
    echo "✅ Migrations completed successfully!"
else
    echo "❌ Migration failed!"
    exit 1
fi
