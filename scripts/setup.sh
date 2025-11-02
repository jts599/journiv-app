#!/bin/bash

# Journal App Setup Script
set -e

echo "🚀 Setting up Journal App Backend..."

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if Docker Compose is installed
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env file from template..."
    if [ -f env.example ]; then
        cp env.example .env
        echo "✅ .env file created. Please edit it with your configuration."
    else
        echo "❌ env.example file not found. Cannot create .env file."
        exit 1
    fi
else
    echo "✅ .env file already exists."
fi

# Create required directories
echo "📁 Creating required directories..."
mkdir -p media logs data

# Determine compose file (default to development)
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"

# Check if compose file exists
if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "❌ Compose file not found: $COMPOSE_FILE"
    echo "Available files:"
    ls -la docker-compose*.yml 2>/dev/null || echo "No docker-compose files found"
    exit 1
fi

echo "📋 Using compose file: $COMPOSE_FILE"

# Start services
echo "🐳 Starting Docker services..."
if docker-compose -f "$COMPOSE_FILE" up -d; then
    echo "✅ Services started successfully."
else
    echo "❌ Failed to start services."
    exit 1
fi

# Wait for services to be ready
echo "⏳ Waiting for services to be ready..."
sleep 15

# Check if services are running
if ! docker-compose -f "$COMPOSE_FILE" ps | grep -q "Up"; then
    echo "❌ Services are not running properly."
    echo "Service status:"
    docker-compose -f "$COMPOSE_FILE" ps
    exit 1
fi

# Run database migrations
echo "🗄️ Running database migrations..."
if docker-compose -f "$COMPOSE_FILE" exec app alembic upgrade head; then
    echo "✅ Database migrations completed successfully."
else
    echo "❌ Database migrations failed."
    exit 1
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "🌐 Your Journal App Backend is ready!"
echo "   - API: http://localhost:8000"
echo "   - Docs: http://localhost:8000/docs"
echo "   - ReDoc: http://localhost:8000/redoc"
echo ""
echo "📋 Next steps:"
echo "   - Edit .env file with your configuration"
echo "   - Use ./scripts/deploy.sh for future deployments"
echo ""
echo "🚀 Quick commands:"
echo "   - Start: ./scripts/deploy.sh --env development"
echo "   - Stop:  docker-compose -f $COMPOSE_FILE down"
echo "   - Logs:  docker-compose -f $COMPOSE_FILE logs -f"
