#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ENV="$ROOT_DIR/deploy/.env"
DEPLOY_ENV_EXAMPLE="$ROOT_DIR/deploy/.env.example"
BACKEND_ENV="$ROOT_DIR/backend/.env"
COMPOSE_FILE="$ROOT_DIR/compose.yaml"
SEED_DIR="$ROOT_DIR/deploy/seed"

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_deploy_env() {
  if [[ ! -f "$DEPLOY_ENV" ]]; then
    if [[ -f "$DEPLOY_ENV_EXAMPLE" ]]; then
      cp "$DEPLOY_ENV_EXAMPLE" "$DEPLOY_ENV"
    fi
    die "Created deploy/.env. Fill it in, then run this script again."
  fi

  set -a
  # shellcheck disable=SC1090
  source "$DEPLOY_ENV"
  set +a
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi

  if [[ "$(uname -s)" != "Linux" ]]; then
    die "Docker is not installed. Install Docker with Compose, then rerun this script."
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    die "Automatic Docker install only supports Ubuntu/Debian hosts with apt-get."
  fi

  local sudo_cmd=()
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    sudo_cmd=(sudo)
  fi

  log "Installing Docker Engine and Compose plugin"
  "${sudo_cmd[@]}" apt-get update
  "${sudo_cmd[@]}" apt-get install -y ca-certificates curl gnupg
  "${sudo_cmd[@]}" install -m 0755 -d /etc/apt/keyrings

  if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | "${sudo_cmd[@]}" gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    "${sudo_cmd[@]}" chmod a+r /etc/apt/keyrings/docker.gpg
  fi

  # shellcheck disable=SC1091
  . /etc/os-release
  local arch
  arch="$(dpkg --print-architecture)"
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' "$arch" "${VERSION_CODENAME:-noble}" \
    | "${sudo_cmd[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null

  "${sudo_cmd[@]}" apt-get update
  "${sudo_cmd[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  "${sudo_cmd[@]}" systemctl enable --now docker
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    printf 'docker'
  else
    printf 'sudo docker'
  fi
}

detect_public_ip() {
  if [[ -n "${EC2_PUBLIC_IP:-}" ]]; then
    printf '%s' "$EC2_PUBLIC_IP"
    return
  fi

  local token ip
  token="$(curl -fsS -m 2 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token 2>/dev/null || true)"

  if [[ -n "$token" ]]; then
    ip="$(curl -fsS -m 2 \
      -H "X-aws-ec2-metadata-token: $token" \
      http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
  else
    ip="$(curl -fsS -m 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
  fi

  if [[ -z "${ip:-}" ]]; then
    ip="$(curl -fsS -m 3 https://checkip.amazonaws.com 2>/dev/null || true)"
  fi

  printf '%s' "$ip" | tr -d '[:space:]'
}

existing_backend_env_value() {
  local key="$1"
  if [[ -f "$BACKEND_ENV" ]]; then
    awk -F= -v k="$key" '$1 == k {print substr($0, length(k) + 2); exit}' "$BACKEND_ENV"
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  fi
}

build_cors_origins() {
  local public_ip="$1"
  local port="${NGINX_PORT:-80}"
  local public_origin
  if [[ "$port" == "80" ]]; then
    public_origin="http://${public_ip}"
  else
    public_origin="http://${public_ip}:${port}"
  fi

  printf '["%s","http://localhost","http://127.0.0.1"]' "$public_origin"
}

write_backend_env() {
  local public_ip="$1"
  local jwt_secret="${JWT_SECRET_KEY:-}"
  if [[ -z "$jwt_secret" ]]; then
    jwt_secret="$(existing_backend_env_value JWT_SECRET_KEY || true)"
  fi
  if [[ -z "$jwt_secret" || "$jwt_secret" == "change-me-in-production" ]]; then
    jwt_secret="$(generate_secret)"
  fi

  [[ -n "${OPENAI_API_KEY:-}" ]] || die "Set OPENAI_API_KEY in deploy/.env."
  [[ -n "$public_ip" ]] || die "Set EC2_PUBLIC_IP in deploy/.env or run this on EC2 with metadata access."

  local cors_origins
  cors_origins="${BACKEND_CORS_ORIGINS:-$(build_cors_origins "$public_ip")}"

  log "Writing backend/.env for production"
  cat >"$BACKEND_ENV" <<EOF
PROJECT_NAME=${PROJECT_NAME:-rag_chatbot_backend}
ENVIRONMENT=production
DEBUG=false
API_V1_PREFIX=${API_V1_PREFIX:-/api/v1}
MONGODB_URI=mongodb://mongodb:27017
MONGODB_DB_NAME=${MONGODB_DB_NAME:-rag_chatbot_backend}
JWT_SECRET_KEY=${jwt_secret}
JWT_ALGORITHM=${JWT_ALGORITHM:-HS256}
ACCESS_TOKEN_EXPIRE_MINUTES=${ACCESS_TOKEN_EXPIRE_MINUTES:-1440}
BACKEND_CORS_ORIGINS=${cors_origins}
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=${QDRANT_API_KEY:-}
QDRANT_COLLECTION_NAME=${QDRANT_COLLECTION_NAME:-rag_documents}
STORAGE_DIR=${STORAGE_DIR:-storage}
MAX_UPLOAD_SIZE_MB=${MAX_UPLOAD_SIZE_MB:-25}
EMBEDDING_DIMENSION=${EMBEDDING_DIMENSION:-1536}
DOCUMENT_CHUNK_SIZE=${DOCUMENT_CHUNK_SIZE:-1200}
DOCUMENT_CHUNK_OVERLAP=${DOCUMENT_CHUNK_OVERLAP:-200}
DOCUMENT_CHUNK_STRATEGY=${DOCUMENT_CHUNK_STRATEGY:-auto}
SCHEDULER_ENABLED=${SCHEDULER_ENABLED:-true}
SCHEDULER_TICK_SECONDS=${SCHEDULER_TICK_SECONDS:-60}
WEBSITE_CRAWL_TIMEOUT_SECONDS=${WEBSITE_CRAWL_TIMEOUT_SECONDS:-20}
WEBSITE_MAX_HTML_BYTES=${WEBSITE_MAX_HTML_BYTES:-2000000}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-https://api.openai.com/v1}
OPENAI_REALTIME_API_BASE=${OPENAI_REALTIME_API_BASE:-https://api.openai.com/v1}
OPENAI_REALTIME_MODEL=${OPENAI_REALTIME_MODEL:-gpt-realtime-2}
OPENAI_REALTIME_VOICE=${OPENAI_REALTIME_VOICE:-marin}
OPENAI_REALTIME_REASONING_EFFORT=${OPENAI_REALTIME_REASONING_EFFORT:-low}
OPENAI_REALTIME_TRANSCRIPTION_MODEL=${OPENAI_REALTIME_TRANSCRIPTION_MODEL:-gpt-4o-mini-transcribe}
OPENAI_REALTIME_NOISE_REDUCTION=${OPENAI_REALTIME_NOISE_REDUCTION:-near_field}
OPENAI_REALTIME_REQUEST_TIMEOUT_SECONDS=${OPENAI_REALTIME_REQUEST_TIMEOUT_SECONDS:-60}
OPENAI_REALTIME_MINT_MAX_RETRIES=${OPENAI_REALTIME_MINT_MAX_RETRIES:-2}
OPENAI_REALTIME_MINT_BACKOFF_BASE_SECONDS=${OPENAI_REALTIME_MINT_BACKOFF_BASE_SECONDS:-0.25}
OPENAI_REALTIME_MINT_LIMIT_PER_USER=${OPENAI_REALTIME_MINT_LIMIT_PER_USER:-20}
OPENAI_REALTIME_MINT_LIMIT_WINDOW_SECONDS=${OPENAI_REALTIME_MINT_LIMIT_WINDOW_SECONDS:-60}
OPENAI_REALTIME_MAX_OUTPUT_TOKENS=${OPENAI_REALTIME_MAX_OUTPUT_TOKENS:-inf}
OPENAI_REALTIME_VAD_TYPE=${OPENAI_REALTIME_VAD_TYPE:-semantic_vad}
OPENAI_REALTIME_VAD_INTERRUPT_RESPONSE=${OPENAI_REALTIME_VAD_INTERRUPT_RESPONSE:-false}
OPENAI_REALTIME_VAD_CREATE_RESPONSE=${OPENAI_REALTIME_VAD_CREATE_RESPONSE:-true}
OPENAI_REALTIME_VAD_THRESHOLD=${OPENAI_REALTIME_VAD_THRESHOLD:-0.78}
OPENAI_REALTIME_VAD_PREFIX_PADDING_MS=${OPENAI_REALTIME_VAD_PREFIX_PADDING_MS:-350}
OPENAI_REALTIME_VAD_SILENCE_MS=${OPENAI_REALTIME_VAD_SILENCE_MS:-650}
LLM_MODEL=${LLM_MODEL:-gpt-4o-mini}
LLM_REQUEST_TIMEOUT_SECONDS=${LLM_REQUEST_TIMEOUT_SECONDS:-120}
LLM_MAX_MESSAGES=${LLM_MAX_MESSAGES:-40}
CHAT_ROUTER_MODEL=${CHAT_ROUTER_MODEL:-gpt-4o-mini}
CHAT_RESPONDER_MODEL=${CHAT_RESPONDER_MODEL:-gpt-4o-mini}
CHAT_HISTORY_FETCH_LIMIT=${CHAT_HISTORY_FETCH_LIMIT:-20}
CHAT_ROUTER_MAX_MESSAGES=${CHAT_ROUTER_MAX_MESSAGES:-20}
CHAT_RAG_TOP_K=${CHAT_RAG_TOP_K:-5}
CHAT_RAG_MIN_SCORE=${CHAT_RAG_MIN_SCORE:-0.25}
CHAT_RAG_VOICE_TOP_K=${CHAT_RAG_VOICE_TOP_K:-3}
CHAT_RAG_VOICE_EXCERPT_CHARS=${CHAT_RAG_VOICE_EXCERPT_CHARS:-1500}
CHAT_RESPONDER_TEMPERATURE=${CHAT_RESPONDER_TEMPERATURE:-0.2}
CHAT_SESSION_TITLE_MODEL=${CHAT_SESSION_TITLE_MODEL:-gpt-4o-mini}
CHAT_SESSION_TITLE_MAX_TOKENS=${CHAT_SESSION_TITLE_MAX_TOKENS:-64}
CHAT_SESSION_TITLE_TEMPERATURE=${CHAT_SESSION_TITLE_TEMPERATURE:-0.2}
CHAT_SESSION_TITLE_PROMPT_MAX_CHARS=${CHAT_SESSION_TITLE_PROMPT_MAX_CHARS:-4000}
OPENAI_EMBEDDING_MODEL=${OPENAI_EMBEDDING_MODEL:-text-embedding-3-small}
OPENAI_EMBEDDING_BATCH_SIZE=${OPENAI_EMBEDDING_BATCH_SIZE:-64}
OPENAI_EMBEDDING_TIMEOUT_SECONDS=${OPENAI_EMBEDDING_TIMEOUT_SECONDS:-120}
ZIP_SESSION_TTL_HOURS=${ZIP_SESSION_TTL_HOURS:-24}
ZIP_INGEST_PATH_BATCH_DEFAULT=${ZIP_INGEST_PATH_BATCH_DEFAULT:-50}
ZIP_INGEST_MAX_PATH_BATCH=${ZIP_INGEST_MAX_PATH_BATCH:-500}
ZIP_INGEST_MAX_PATH_INDICES=${ZIP_INGEST_MAX_PATH_INDICES:-500}
ZIP_INGEST_MAX_MARKDOWN_LISTED=${ZIP_INGEST_MAX_MARKDOWN_LISTED:-100000}
ZIP_INGEST_MAX_UNCOMPRESSED_BYTES=${ZIP_INGEST_MAX_UNCOMPRESSED_BYTES:-52428800}
ZIP_INGEST_MAX_ENTRY_BYTES=${ZIP_INGEST_MAX_ENTRY_BYTES:-8388608}
EOF
}

wait_for_backend() {
  local docker_binary="$1"
  log "Waiting for backend health check"

  for _ in $(seq 1 60); do
    local status
    status="$($docker_binary inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' dds_rag_backend 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      return
    fi
    sleep 5
  done

  $docker_binary compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" logs --tail=150 backend >&2 || true
  die "Backend did not become healthy."
}

main() {
  ensure_deploy_env
  ensure_docker
  mkdir -p "$SEED_DIR"

  local public_ip docker_binary
  public_ip="$(detect_public_ip)"
  write_backend_env "$public_ip"

  docker_binary="$(docker_cmd)"

  log "Building and starting the production stack"
  $docker_binary compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" up -d --build

  wait_for_backend "$docker_binary"

  log "Running optional seed document ingestion"
  "$ROOT_DIR/scripts/ingest_seed_docs.sh"

  log "Deployment complete"
  printf 'Website: http://%s%s\n' "$public_ip" "$([[ "${NGINX_PORT:-80}" == "80" ]] && printf '' || printf ':%s' "${NGINX_PORT}")"
  printf 'Health:  http://%s%s/api/v1/health\n' "$public_ip" "$([[ "${NGINX_PORT:-80}" == "80" ]] && printf '' || printf ':%s' "${NGINX_PORT}")"
}

main "$@"
