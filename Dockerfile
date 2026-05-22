# ================================================================
# Betpreneur Backend Dockerfile
# ================================================================
# Multi-stage build for production-ready image
# ================================================================

# ------------------------------
# Stage 1: Base
# ------------------------------
FROM python:3.12-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Africa/Lagos

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # PostgreSQL client for migrations
    libpq-dev \
    # Fonts for PDF generation (ReportLab)
    fonts-dejavu-core \
    # Gunicorn
    gunicorn \
    # Docker healthcheck
    curl \
    # Clean up
    && rm -rf /var/lib/apt/lists/*


# ------------------------------
# Stage 2: Dependencies
# ------------------------------
FROM base AS dependencies

# Create virtual environment
RUN python -m venv /pyenv
ENV PATH="/pyenv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ------------------------------
# Stage 3: Application
# ------------------------------
FROM base AS application

# Copy virtual environment from dependencies stage
COPY --from=dependencies /pyenv /pyenv
ENV PATH="/pyenv/bin:$PATH"

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /home/appuser/.cache/pip && \
    chown -R appuser:appuser /home/appuser

# Switch to non-root user
USER appuser
WORKDIR /home/appuser

# Copy application code
COPY --chown=appuser:appuser . .

# Create cache directory for pip
RUN mkdir -p /home/appuser/.cache/pip /home/appuser/staticfiles /home/appuser/media


# ------------------------------
# Stage 4: Production
# ------------------------------
FROM application AS production

# Expose port
EXPOSE 8000

# Run migrations, collect static assets, and start server
CMD ["sh", "-c", "echo '[startup] running migrations' && python manage.py migrate --noinput && echo '[startup] collecting static files' && python manage.py collectstatic --noinput && echo '[startup] starting gunicorn on 0.0.0.0:8000' && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-2} --worker-class gthread --threads ${WEB_THREADS:-2} --timeout ${GUNICORN_TIMEOUT:-120} --graceful-timeout 30 --keep-alive 5 --access-logfile - --error-logfile -"]
