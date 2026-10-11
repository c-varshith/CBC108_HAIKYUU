#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <backup-directory>" >&2
  exit 2
fi

backup_dir=$1
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup_dir"
env_file=${CALIBREX_ENV_FILE:-.env.production}
compose=(docker compose --env-file "$env_file" -f docker-compose.yml -f docker-compose.production.yml --profile ai)

"${compose[@]}" exec -T postgres pg_dump -U calibrex -d calibrex -Fc \
  > "$backup_dir/calibrex-db-$stamp.dump"
"${compose[@]}" exec -T api tar -C /srv/data -czf - . \
  > "$backup_dir/calibrex-images-$stamp.tar.gz"

echo "Wrote database and local image backups with timestamp $stamp to $backup_dir"
