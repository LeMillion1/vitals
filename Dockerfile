FROM python:3.13-slim

# Sync timezone to Chisinau wall-clock time
ENV TZ=Europe/Chisinau
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -sf /usr/share/zoneinfo/Europe/Chisinau /etc/localtime \
    && echo "Europe/Chisinau" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Launch FastAPI by default. Compose overrides this command for the standalone
# scheduler worker and runs Alembic/role provisioning as separate one-shot steps.
# --forwarded-allow-ips="*": trust X-Forwarded-For so per-IP login throttling
# sees the real client IP, not Caddy's. Safe here — the port binds loopback only
# and Caddy is the sole upstream.
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips=*"]
