FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY . .

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python healthcheck.py

CMD ["python", "shift_bot.py"]
