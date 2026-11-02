#!/usr/bin/env bash
# Usage:
#   bash deploy.sh          — rebuild both frontend and backend
#   bash deploy.sh frontend — rebuild frontend only
#   bash deploy.sh backend  — rebuild backend only

set -e

TARGET=${1:-all}

pull_latest() {
  echo "Pulling latest code..."
  git pull
}

build() {
  local svc=$1
  echo "Building $svc..."
  docker compose up -d --build --no-deps "$svc"
  echo "$svc updated."
}

pull_latest

case "$TARGET" in
  frontend) build frontend ;;
  backend)  build backend ;;
  all)      build backend; build frontend ;;
  *)        echo "Usage: bash deploy.sh [frontend|backend|all]"; exit 1 ;;
esac

echo "Done."
