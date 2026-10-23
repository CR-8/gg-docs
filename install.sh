#!/usr/bin/env bash
# One command to install and start the GeneGenius portal on a fresh Ubuntu or Debian VM, with HTTPS on
# <ip>.sslip.io, so no domain is needed. Safe to re-run: secrets and values already in .env are kept,
# and a re-run pulls the latest code and restarts.
#
#   bash install.sh 35.253.245.77
#
# The GitHub App (sign-in, cloning, webhooks; README 3.1), in one go (otherwise edit .env later and re-run):
#   GITHUB_APP_ID=123456 GITHUB_CLIENT_ID=Iv23li... GITHUB_CLIENT_SECRET=... \
#   APP_KEY=~/genegenius-reviewer.private-key.pem  bash install.sh 35.253.245.77
# Email for team invites (any SMTP provider; without it, invite links are copied by hand):
#   SMTP_HOST=smtp.resend.com SMTP_USER=resend SMTP_PASSWORD=... SMTP_FROM=portal@yourdomain bash install.sh <ip>
# Run a coder model on this VM for reviews and the chatbot (Ollama, Qwen2.5-Coder Q8 sized to the RAM):
#   LOCAL_LLM=auto bash install.sh 35.253.245.77        # or an Ollama tag, e.g. qwen2.5-coder:7b-instruct-q8_0
#   LOCAL_LLM=off  bash install.sh 35.253.245.77        # switch it off again
set -euo pipefail

IP="${1:-${PUBLIC_IP:-}}"
[ -n "$IP" ] || IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "Usage: bash install.sh <public-ip>" >&2; exit 1; }
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"   # sslip.io resolves 35-253-245-77.sslip.io to 35.253.245.77
REPO_URL="${REPO_URL:-https://github.com/CR-8/gg-docs.git}"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Docker"
if ! command -v docker >/dev/null 2>&1; then
  . /etc/os-release
  case "$ID" in ubuntu|debian) ;; *) echo "Unsupported OS '$ID': install Docker yourself, then re-run." >&2; exit 1 ;; esac
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq ca-certificates curl git openssl
  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
  if [ -n "$SUDO" ]; then $SUDO usermod -aG docker "$USER"; fi   # docker without sudo from your next login
fi
command -v git >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1 || { $SUDO apt-get update -qq; $SUDO apt-get install -y -qq git openssl curl; }
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"

say "Container network"
# The portal, Caddy (certificates) and the backend venv all need internet from inside containers.
probe() { $DOCKER run --rm python:3.11-slim-bookworm python -c \
  "import urllib.request; urllib.request.urlopen('https://deb.debian.org', timeout=15)" >/dev/null 2>&1; }
if ! probe; then
  if ! $SUDO test -s /etc/docker/daemon.json; then
    echo "Containers can't resolve names; pointing Docker at public DNS (8.8.8.8, 1.1.1.1) and retrying."
    echo '{"dns": ["8.8.8.8", "1.1.1.1"]}' | $SUDO tee /etc/docker/daemon.json >/dev/null
    $SUDO systemctl restart docker
  fi
  if ! probe; then
    echo "Containers still can't reach https://deb.debian.org. Share this output when asking for help:" >&2
    { echo "--- host resolv.conf"; cat /etc/resolv.conf; echo "--- routes"; ip -4 route
      echo "--- daemon.json"; $SUDO cat /etc/docker/daemon.json 2>/dev/null
      echo "--- inside a container"
      $DOCKER run --rm python:3.11-slim-bookworm sh -c 'cat /etc/resolv.conf; getent hosts deb.debian.org || echo "DNS failed"'
    } >&2
    exit 1
  fi
fi
echo "containers reach the internet"

say "Kernel setting SonarQube needs"
echo 'vm.max_map_count=524288' | $SUDO tee /etc/sysctl.d/99-sonarqube.conf >/dev/null
$SUDO sysctl -q -w vm.max_map_count=524288

say "Code"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"
if [ -f "$HERE/docker-compose.yml" ]; then
  DIR="$HERE"
else
  DIR="${INSTALL_DIR:-$HOME/gg-portal}"
  [ -d "$DIR/.git" ] || git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"
[ -d .git ] && { git pull --ff-only || echo "(not updating: local changes or diverged history)"; }
echo "$DIR"

say ".env"
[ -f .env ] || cp .env.example .env
getkv() { grep -m1 "^$1=" .env | cut -d= -f2- || true; }
setkv() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
fill() { [ -n "$(getkv "$1")" ] || setkv "$1" "$2"; }          # only when still empty
setkv PUBLIC_URL "https://$DOMAIN"                                # follows the IP on every run
setkv DOMAIN "$DOMAIN"
setkv BIND_ADDR 127.0.0.1                                         # 7000/7010 stay private; Caddy serves 443
setkv SONAR_PUBLIC_URL "https://$DOMAIN/sonar"
fill SESSION_SECRET "$(openssl rand -hex 32)"
fill TOKEN_ENCRYPTION_KEY "$(openssl rand -hex 32)"
fill SONAR_DB_PASSWORD "$(openssl rand -hex 24)"
fill GITHUB_WEBHOOK_SECRET "$(openssl rand -hex 32)"
for k in GITHUB_TOKEN GITHUB_APP_ID GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET ADMINS EXTRA_REPOS \
         SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_FROM; do
  if [ -n "${!k:-}" ]; then setkv "$k" "${!k}"; fi
done

# Local model: pick the biggest Qwen2.5-Coder Q8 that fits next to SonarQube (~3 GB) and the portal (~2 GB).
LLM_TAG=""
if [ "${LOCAL_LLM:-}" = "off" ]; then
  setkv COMPOSE_PROFILES https
  for k in LLM_KIND LLM_BASE_URL LLM_MODEL LLM_API_KEY LLM_TIMEOUT REVIEW_CHUNK_CHARS REVIEW_MAX_CHARS CHAT_TOOL_CHARS DOCS_CONTEXT_CHARS \
           CHAT_TIMEOUT LLM_PARALLEL; do
    setkv "$k" ""
  done
elif [ -n "${LOCAL_LLM:-}" ]; then
  ram_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  if [ "$LOCAL_LLM" != "auto" ]; then LLM_TAG="$LOCAL_LLM"
  elif [ "$ram_gb" -ge 15 ]; then LLM_TAG="qwen2.5-coder:7b-instruct-q8_0"     # ~8 GB in RAM
  elif [ "$ram_gb" -ge 10 ]; then LLM_TAG="qwen2.5-coder:3b-instruct-q8_0"     # ~3.5 GB
  else LLM_TAG="qwen2.5-coder:1.5b-instruct-q8_0"                               # ~2 GB
  fi
  echo "Local model: $LLM_TAG (VM has ${ram_gb} GB RAM)"
  # Models an Ollama install on this VM already downloaded are reused instead of downloaded again.
  for d in /usr/share/ollama/.ollama "$HOME/.ollama"; do
    if [ -z "$(getkv OLLAMA_DIR)" ] && $SUDO test -d "$d/models"; then setkv OLLAMA_DIR "$d"; echo "Reusing the models in $d"; fi
  done
  ctx="${LLM_CONTEXT:-16384}"
  setkv COMPOSE_PROFILES "https,llm"
  setkv LLM_CONTEXT "$ctx"
  setkv LLM_KIND openai
  setkv LLM_BASE_URL "http://llm:11434/v1"
  setkv LLM_MODEL "$LLM_TAG"
  setkv LLM_API_KEY ollama                                         # Ollama ignores it; the client needs one
  setkv LLM_EFFORT ""
  setkv LLM_TIMEOUT 3600                                           # CPU inference is slow
  setkv REVIEW_CHUNK_CHARS $(( (ctx - 4000) * 3 ))                 # leave room for instructions and the answer
  setkv REVIEW_MAX_CHARS $(( (ctx - 4000) * 12 ))
  setkv CHAT_TOOL_CHARS 6000
  setkv CHAT_TIMEOUT 900                                            # a whole answer, all its lookups included
  setkv LLM_PARALLEL "$([ "$ram_gb" -ge 12 ] && echo 2 || echo 1)"   # a second slot (Ask never waits) needs the RAM
  setkv DOCS_CONTEXT_CHARS $(( (ctx - 5000) * 3 ))                # code facts per documentation request
fi
fill COMPOSE_PROFILES https
# A plain re-run keeps the local model and finishes a download an earlier run didn't.
if [ -z "$LLM_TAG" ] && [[ "$(getkv COMPOSE_PROFILES)" == *llm* ]]; then LLM_TAG="$(getkv LLM_MODEL)"; fi
chmod 600 .env

say "GitHub App private key (optional)"
mkdir -p secrets
if [ -n "${APP_KEY:-}" ]; then $SUDO cp "${APP_KEY/#\~/$HOME}" secrets/github-app.pem; fi
# The container runs as uid 1000; give it the key (and nobody else).
$SUDO chown -R 1000:1000 secrets && $SUDO chmod 700 secrets
$SUDO test -f secrets/github-app.pem && $SUDO chmod 600 secrets/github-app.pem && echo "secrets/github-app.pem in place" \
  || echo "no GitHub App key (fine: the App is optional)"

say "Starting (the first build takes a few minutes)"
$DOCKER compose up -d --build --remove-orphans     # COMPOSE_PROFILES in .env picks https / llm
if [ "${LOCAL_LLM:-}" = "off" ]; then $DOCKER compose --profile llm rm -sf llm; fi

if [ -n "$LLM_TAG" ]; then   # while Caddy gets its certificate
  say "Local model $LLM_TAG"
  for _ in $(seq 1 30); do $DOCKER compose exec -T llm ollama list >/dev/null 2>&1 && break; sleep 2; done
  if $DOCKER compose exec -T llm ollama show "$LLM_TAG" >/dev/null 2>&1; then
    echo "already downloaded"
  elif $DOCKER compose exec -T llm ollama pull "$LLM_TAG"; then
    echo "downloaded. CPU-only answers take a while: tens of seconds for chat, minutes per review."
  else
    echo "The download failed or was interrupted; re-run install.sh to resume it." >&2
  fi
fi

say "Waiting for https://$DOMAIN"
ok=""
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null; then ok=1; break; fi
  sleep 5
done
if [ -n "$ok" ]; then
  echo "Up: https://$DOMAIN"
else
  echo "Not reachable over HTTPS yet. Check that the VM firewall allows TCP 80 and 443 from anywhere"
  echo "(Let's Encrypt must reach port 80), then: $DOCKER compose logs caddy"
fi

app_ready=""
if [ -n "$(getkv GITHUB_APP_ID)" ] && $SUDO test -f secrets/github-app.pem; then app_ready=1; fi

cat <<EOF

Portal     https://$DOMAIN   (Continue with GitHub, create a workspace, import repositories)
SonarQube  https://$DOMAIN/sonar   (first login admin / admin, then change it)
MCP        each workspace has its own: Settings -> Connect a coding agent
EOF
if [ -n "$(getkv SMTP_HOST)" ]; then echo "Invites    emailed through $(getkv SMTP_HOST)"
else echo "Invites    no SMTP_HOST, so people copy invite links from Settings"; fi
if [ -n "$LLM_TAG" ]; then
  echo "Local model  $LLM_TAG   (Settings lists every downloaded model; one click switches to it)"
fi
if [ -z "$app_ready" ]; then
  cat <<EOF

Needed: the GitHub App. People sign in with it, and it clones the repositories they import and sends their webhooks.
Create it (README 3.1) with:
  Homepage URL    https://$DOMAIN
  Callback URL    https://$DOMAIN/auth/callback
  Setup URL       https://$DOMAIN/new/   (tick "Redirect on update")
  Webhook URL     https://$DOMAIN/webhook/github
  Webhook secret  $(getkv GITHUB_WEBHOOK_SECRET)
  Installable by  Any account
then re-run with GITHUB_APP_ID, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET and APP_KEY (see the top of this file).
EOF
fi
