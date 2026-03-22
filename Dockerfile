FROM python:3.13-slim

LABEL maintainer="xozy22"
LABEL description="FortiLink FortiSwitch Fiber Diagnose Tool"

WORKDIR /app

# Install system dependencies (ping for host connectivity check)
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application
COPY app.py .
COPY static/ static/

EXPOSE 5000

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "8", "--timeout", "120", "app:app"]
