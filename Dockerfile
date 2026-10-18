# GeneGenius portal: FastAPI app + sync worker, with git, Node (dependency-cruiser) and sonar-scanner.
FROM node:22-bookworm-slim AS node

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# sonar-scanner with its own JRE, so the image needs no Java
ARG SCANNER=8.1.0.6389
RUN curl -fsSL -o /tmp/scanner.zip \
      https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SCANNER}-linux-x64.zip \
    && unzip -q /tmp/scanner.zip -d /opt && mv /opt/sonar-scanner-${SCANNER}-linux-x64 /opt/sonar-scanner \
    && rm /tmp/scanner.zip
ENV PATH=/opt/sonar-scanner/bin:$PATH

WORKDIR /app
COPY requirements.txt package.json package-lock.json ./
RUN pip install --no-cache-dir -r requirements.txt && npm ci --no-audit --no-fund
COPY build.py depcruise.cjs ./
COPY portal ./portal

RUN useradd --create-home portal && mkdir /data && chown portal /data
USER portal
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 GRAPHIFY_NO_AUTO_REFRESH=1
VOLUME /data
EXPOSE 7000
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "7000"]
