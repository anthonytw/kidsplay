#!/bin/bash
# Pull the latest code and rebuild/restart the server container.
set -e
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d
echo "Updated and restarted."
