# Restore a local CALIBREX backup

These steps restore the local-storage deployment created by `docker-compose.production.yml`. They overwrite the active database and image files; take a fresh backup first and schedule downtime.

The commands below pass `.env.production` explicitly. The backup script does the same by default; set `CALIBREX_ENV_FILE` to use a different path.

1. Stop the API and worker so no scans or catalog updates occur during restore:

   ```bash
   docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile ai stop api ai-worker
   ```

2. Restore the PostgreSQL dump (replace the filenames with one matching backup pair):

   ```bash
   docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile ai exec -T postgres \
     pg_restore --clean --if-exists --no-owner -U calibrex -d calibrex < calibrex-db-YYYYMMDDTHHMMSSZ.dump
   ```

3. Restore the image volume:

   ```bash
   docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile ai run --rm --no-deps \
     --entrypoint tar api -C /srv/data -xzf - < calibrex-images-YYYYMMDDTHHMMSSZ.tar.gz
   ```

4. Start the stack and check `/api/health/ready`. If Qdrant is missing or empty, run the catalog resync from the API container:

   ```bash
   docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml --profile ai exec -T api python -m scripts.resync_qdrant
   ```

For `STORAGE_BACKEND=s3`, this local image archive is not a backup of the bucket. Enable versioning/replication and restore the reference-image objects with the storage provider before starting CALIBREX. Qdrant vectors can be reconstructed from the database and reference images.
