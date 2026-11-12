# Genisi portable Docker image: Node 22 for the Next.js workspace + Python 3.11 for FastAPI.
FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt

COPY app/ /app/app/
COPY static/ /app/static/

# The official Node image already provides the non-root `node` account (UID 1000).
RUN mkdir -p /app/data /tmp/genisi-workspaces \
    && chown -R node:node /app /tmp/genisi-workspaces
USER node
ENV HOME=/home/node

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
