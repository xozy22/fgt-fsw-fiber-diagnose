FROM python:3.13-slim

LABEL maintainer="xozy22"
LABEL description="FortiLink FortiSwitch Fiber Diagnose Tool"

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application
COPY app.py .
COPY static/ static/

EXPOSE 5000

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "8", "--timeout", "120", "app:app"]
