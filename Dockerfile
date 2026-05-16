# ─── Bot container ───
FROM python:3.12-slim AS bot

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 1000 botuser && \
    useradd -u 1000 -g botuser -m -s /bin/false botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory for SQLite
RUN mkdir -p /app/data && chown botuser:botuser /app/data

USER botuser

CMD ["python", "-m", "bot.main"]
