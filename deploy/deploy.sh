#!/bin/bash
set -e

docker buildx bake \
  -f compose.yaml \
  --allow=network.host \
  --load \
  --progress=plain \
  be-data-analysis

docker compose up -d --no-build
docker compose ps
