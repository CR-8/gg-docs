# gg portal: FastAPI app + sync worker, with git and Node (dependency-cruiser).
FROM node:22-bookworm-slim AS node

# The web frontend (Astro + shadcn/ui), built to static files the portal serves
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

WORKDIR /app
COPY requirements.txt package.json package-lock.json ./
RUN pip install --no-cache-dir -r requirements.txt && npm ci --no-audit --no-fund
COPY build.py depcruise.cjs ./
COPY portal ./portal
COPY --from=web /web/dist ./web/dist

RUN useradd --create-home portal && mkdir /data && chown portal /data
USER portal
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 GRAPHIFY_NO_AUTO_REFRESH=1
VOLUME /data
EXPOSE 7000
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "7000"]
