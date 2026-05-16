# ─── Bot container ───
FROM python:3.12-slim AS bot

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Docker CLI binary so the bot can spawn sibling runner containers (DooD).
# We pull it from the official docker:cli image instead of installing the
# whole docker.io package (~80 MB) or the docker-ce-cli apt repo.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# Create non-root user. DOCKER_GID is the host's docker group GID — pass it
# at build time so the in-container user can read /var/run/docker.sock.
# Common values: Debian/Ubuntu = 999 or 998. Override with --build-arg or
# DOCKER_GID in .env (picked up by docker-compose.yml).
ARG DOCKER_GID=999
RUN groupadd -g 1000 botuser && \
    useradd -u 1000 -g botuser -m -s /bin/false botuser && \
    if ! getent group ${DOCKER_GID} >/dev/null; then \
        groupadd -g ${DOCKER_GID} docker; \
    fi && \
    usermod -aG $(getent group ${DOCKER_GID} | cut -d: -f1) botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory for SQLite
RUN mkdir -p /app/data && chown botuser:botuser /app/data

USER botuser

CMD ["python", "-m", "bot.main"]
