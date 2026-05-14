#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ENV="$ROOT_DIR/deploy/.env"
COMPOSE_FILE="$ROOT_DIR/compose.yaml"
SEED_DIR="$ROOT_DIR/deploy/seed"
SEED_ZIP="$SEED_DIR/docs.zip"
INGEST_MARKER="$SEED_DIR/.docs_zip.sha256"

log() {
  printf '[ingest] %s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

checksum_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    python3 - "$file" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
h = hashlib.sha256()
with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
  fi
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    printf 'docker'
  else
    printf 'sudo docker'
  fi
}

resolve_docs_zip() {
  local configured="$1"
  if [[ "$configured" = /* ]]; then
    printf '%s' "$configured"
  else
    printf '%s/%s' "$ROOT_DIR" "$configured"
  fi
}

main() {
  if [[ ! -f "$DEPLOY_ENV" ]]; then
    die "Missing deploy/.env."
  fi

  set -a
  # shellcheck disable=SC1090
  source "$DEPLOY_ENV"
  set +a

  if [[ -z "${DOCS_ZIP:-}" ]]; then
    log "DOCS_ZIP is unset; skipping seed document ingestion."
    return
  fi

  [[ -n "${INGEST_EMAIL:-}" ]] || die "Set INGEST_EMAIL in deploy/.env when DOCS_ZIP is set."
  [[ -n "${INGEST_PASSWORD:-}" ]] || die "Set INGEST_PASSWORD in deploy/.env when DOCS_ZIP is set."

  local source_zip
  source_zip="$(resolve_docs_zip "$DOCS_ZIP")"
  [[ -f "$source_zip" ]] || die "DOCS_ZIP does not exist: $source_zip"
  [[ "${source_zip##*.}" == "zip" ]] || die "DOCS_ZIP must point to a .zip file."

  mkdir -p "$SEED_DIR"
  cp "$source_zip" "$SEED_ZIP"

  local checksum previous_checksum
  checksum="$(checksum_file "$SEED_ZIP")"
  previous_checksum="$(cat "$INGEST_MARKER" 2>/dev/null || true)"

  if [[ "${FORCE_INGEST:-false}" != "true" && "$checksum" == "$previous_checksum" ]]; then
    log "Seed ZIP checksum already ingested; set FORCE_INGEST=true to run it again."
    return
  fi

  local register_flag=()
  if [[ "${INGEST_REGISTER:-true}" == "true" ]]; then
    register_flag=(--register)
  fi
  local ingest_full_name="${INGEST_FULL_NAME:-DDS Tester}"

  local docker_binary
  docker_binary="$(docker_cmd)"

  log "Running Markdown ZIP ingestion inside the backend container."
  $docker_binary compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" exec -T backend \
    python scripts/ingest_markdown_zip.py /seed/docs.zip \
      --base-url http://127.0.0.1:8000 \
      --email "$INGEST_EMAIL" \
      --password "$INGEST_PASSWORD" \
      --full-name "$ingest_full_name" \
      "${register_flag[@]}"

  printf '%s\n' "$checksum" >"$INGEST_MARKER"
  log "Seed document ingestion complete."
}

main "$@"
