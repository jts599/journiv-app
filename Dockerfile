# =========================
# Stage 1: Builder
# =========================
FROM python:3.11-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONPATH=/app \
  PATH=/root/.local/bin:$PATH

WORKDIR /app

# Install build dependencies (includes ffmpeg)
RUN apk add --no-cache --virtual .build-deps \
  gcc \
  musl-dev \
  libffi-dev \
  postgresql-dev \
  libmagic \
  curl \
  ffmpeg \
  build-base \
  git \
  && echo "🔍 Checking FFmpeg license (builder stage)..." \
  && ffmpeg -version | grep -E "enable-gpl|enable-nonfree" && (echo "❌ GPL/nonfree FFmpeg detected!" && exit 1) || echo "✅ LGPL FFmpeg build verified."

# Copy requirements and install Python deps
COPY requirements/ requirements/
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements/prod.txt

# =========================
# Stage 2: Runtime
# =========================
FROM python:3.11-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONPATH=/app \
  PATH=/root/.local/bin:$PATH \
  ENVIRONMENT=production \
  LOG_LEVEL=INFO

WORKDIR /app

# Install runtime dependencies and verify ffmpeg license
RUN apk add --no-cache \
  libmagic \
  curl \
  ffmpeg \
  libffi \
  postgresql-libs \
  libpq \
  git \
  github-cli \
  bash \
  && echo "🔍 Checking FFmpeg license (runtime stage)..." \
  && ffmpeg -version | grep -E "enable-gpl|enable-nonfree" && (echo "❌ GPL/nonfree FFmpeg detected!" && exit 1) || echo "✅ LGPL FFmpeg build verified."

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy app code and assets
COPY app/ app/

# Copy database migration files
COPY alembic/ alembic/
COPY alembic.ini .

# Copy scripts directory (seed data and entrypoint)
COPY scripts/moods.json scripts/moods.json
COPY scripts/prompts.json scripts/prompts.json
COPY scripts/docker-entrypoint.sh scripts/docker-entrypoint.sh

# Copy prebuilt Flutter web app
COPY web/ web/

# Non-root user and directories
# Create /data directory with subdirectories for media and logs
# Database file (journiv.db) will be created directly in /data/ by SQLite
RUN adduser -D -u 1000 appuser \
  && mkdir -p /data/media /data/logs \
  && chmod +x scripts/docker-entrypoint.sh \
  && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/api/v1/health || exit 1

CMD ["/app/scripts/docker-entrypoint.sh"]

# =========================
# Stage 3: Development
# =========================
FROM runtime AS dev

# Switch back to root to install sudo and configure permissions
USER root

# Install sudo and give appuser permission to use apk for package management
# particularly needed for git-lfs installation
RUN apk add --no-cache sudo \
  && echo "appuser ALL=(ALL) NOPASSWD: /sbin/apk" >> /etc/sudoers

# Switch back to appuser for development work
USER appuser
