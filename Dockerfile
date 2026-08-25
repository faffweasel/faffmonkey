# syntax=docker/dockerfile:1

FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# The process runs as an unprivileged user so that everything it writes to
# the workspace/ and state/ bind mounts is owned by uid 1000 on the host,
# not root. docker-compose.yml overrides the uid/gid for hosts where the
# operator is not 1000.
RUN groupadd --gid 1000 faff \
    && useradd --uid 1000 --gid faff --create-home faff

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
COPY bin/ bin/
COPY templates/ templates/
COPY contrib/ contrib/
# COPY keeps the host's mode bits and makes root the owner, so a file that
# was 0600 or 0711 on the host is unreadable by the faff user. One script
# arrived that way and `faff skill install` failed with EACCES.
RUN chmod -R a+rX src bin templates contrib \
    && chmod +x bin/faff && ln -s /app/bin/faff /usr/local/bin/faff

COPY requirements.extra.txt* ./
RUN if [ -f requirements.extra.txt ]; then \
        pip install --no-cache-dir -r requirements.extra.txt; \
    fi

# Data root inside the container: compose mounts the host's FAFF_HOME
# (default ~/.faffmonkey) at these paths.
ENV FAFF_HOME=/app
ENV PYTHONUNBUFFERED=1

USER faff

CMD ["faff", "run"]
