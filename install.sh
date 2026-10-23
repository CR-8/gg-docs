#!/usr/bin/env bash
# One command to install and start the GeneGenius portal on a fresh Ubuntu or Debian VM, with HTTPS on
# <ip>.sslip.io, so no domain is needed. Safe to re-run: secrets and values already in .env are kept,
# and a re-run pulls the latest code and restarts.
#
#   bash install.sh 35.253.245.77
#
# Optional, to fill the GitHub App values in one go (otherwise edit .env later and re-run):
#   GITHUB_APP_ID=123456 GITHUB_CLIENT_ID=Iv23li... GITHUB_CLIENT_SECRET=... ADMINS=your-login \
#   APP_KEY=~/genegenius-reviewer.private-key.pem  bash install.sh 35.253.245.77
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
fill SONAR_DB_PASSWORD "$(openssl rand -hex 24)"
fill GITHUB_WEBHOOK_SECRET "$(openssl rand -hex 32)"
for k in GITHUB_APP_ID GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET ADMINS; do
  if [ -n "${!k:-}" ]; then setkv "$k" "${!k}"; fi
done
chmod 600 .env

say "GitHub App private key"
mkdir -p secrets
if [ -n "${APP_KEY:-}" ]; then $SUDO cp "${APP_KEY/#\~/$HOME}" secrets/github-app.pem; fi
# The container runs as uid 1000; give it the key (and nobody else).
$SUDO chown -R 1000:1000 secrets && $SUDO chmod 700 secrets
$SUDO test -f secrets/github-app.pem && $SUDO chmod 600 secrets/github-app.pem && echo "secrets/github-app.pem in place" \
  || echo "secrets/github-app.pem missing (GitHub sync and login stay off until you add it)"

say "Starting (the first build takes a few minutes)"
$DOCKER compose --profile https up -d --build

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

missing=()
for k in GITHUB_APP_ID GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET ADMINS; do [ -n "$(getkv "$k")" ] || missing+=("$k in .env"); done
$SUDO test -f secrets/github-app.pem || missing+=("secrets/github-app.pem")

cat <<EOF

Portal     https://$DOMAIN
SonarQube  https://$DOMAIN/sonar   (first login admin / admin, then change it)

GitHub App settings (github.com/organizations/GeneGeniusinc/settings/apps):
  Homepage URL    https://$DOMAIN
  Callback URL    https://$DOMAIN/auth/callback
  Webhook URL     https://$DOMAIN/webhook/github
  Webhook secret  $(getkv GITHUB_WEBHOOK_SECRET)
EOF
if [ ${#missing[@]} -gt 0 ]; then
  printf '\nStill needed, then re-run "bash install.sh %s":\n' "$IP"
  printf '  - %s\n' "${missing[@]}"
fi
