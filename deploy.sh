#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
command -v docker >/dev/null || { echo 'Docker Engine + Compose plugin are required on this server.'; exit 1; }
docker compose version >/dev/null
docker build -t live-asr-backend:local .
if [ ! -f .env ]; then
  docker run --rm -v "$PWD:/config" live-asr-backend:local python init_config.py --directory /config --server "${1:-118.196.114.88}"
fi
mkdir -p data certs
docker compose up -d
docker compose ps
