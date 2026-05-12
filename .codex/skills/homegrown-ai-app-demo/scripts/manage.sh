#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"

usage() {
  cat <<'USAGE'
Usage: manage.sh start|stop|restart|status|health|refresh-models|verify|logs [service]

Environment:
  HOMEGROWN_AI_APP_DIR  Override app directory.
  TAIL                  Override log tail line count for logs.
USAGE
}

looks_like_app_dir() {
  local dir="$1"
  [[ -f "$dir/docker-compose.yml" && -d "$dir/app" && -d "$dir/litellm" ]]
}

find_app_dir() {
  local dir

  if [[ -n "${HOMEGROWN_AI_APP_DIR:-}" ]]; then
    if looks_like_app_dir "$HOMEGROWN_AI_APP_DIR"; then
      printf "%s\n" "$HOMEGROWN_AI_APP_DIR"
      return
    fi
    printf "HOMEGROWN_AI_APP_DIR does not look like this repo: %s\n" "$HOMEGROWN_AI_APP_DIR" >&2
    exit 1
  fi

  dir="$PWD"
  while [[ "$dir" != "/" ]]; do
    if looks_like_app_dir "$dir"; then
      printf "%s\n" "$dir"
      return
    fi
    dir="$(dirname "$dir")"
  done

  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  while [[ "$dir" != "/" ]]; do
    if looks_like_app_dir "$dir"; then
      printf "%s\n" "$dir"
      return
    fi
    dir="$(dirname "$dir")"
  done

  cat >&2 <<'ERROR'
Could not find the HomeGrown AI App Demo repo root.
Run from inside the repo or set HOMEGROWN_AI_APP_DIR=/path/to/homegrown-ai-app-demo.
ERROR
  exit 1
}

if [[ "$ACTION" == "help" || "$ACTION" == "--help" || "$ACTION" == "-h" ]]; then
  usage
  exit 0
fi

APP_DIR="$(find_app_dir)"
cd "$APP_DIR"

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<'ERROR'
Docker is not reachable. Start Docker Desktop or the Docker daemon, then rerun this command.
ERROR
    exit 1
  fi
}

env_value() {
  local key="$1"
  if [[ -f .env ]]; then
    sed -n "s/^${key}=//p" .env | tail -n 1
  fi
}

json_value() {
  local field="$1"
  python3 -c "import json,sys; print(json.load(sys.stdin).get('${field}', ''))"
}

admin_token() {
  local email password response
  email="$(env_value ADMIN_EMAIL)"
  password="$(env_value ADMIN_PASSWORD)"
  email="${email:-admin@example.com}"
  password="${password:-admin}"

  response="$(
    curl -fsS http://localhost:9000/auth/login \
      -H "Content-Type: application/json" \
      -d "{\"email\":\"${email}\",\"password\":\"${password}\"}"
  )"
  printf "%s" "$response" | json_value access_token
}

health() {
  curl -fsS http://localhost:9000/health | python3 -m json.tool
}

refresh_models() {
  local token
  token="$(admin_token)"
  curl -fsS -X POST http://localhost:9000/admin/refresh-models \
    -H "Authorization: Bearer ${token}" | python3 -m json.tool
}

model_count() {
  local token
  token="$(admin_token)"
  curl -fsS http://localhost:9000/models \
    -H "Authorization: Bearer ${token}" |
    python3 -c 'import json,sys; data=json.load(sys.stdin); print(f"{len(data.get(\"models\", []))} models; fallback={data.get(\"fallback\")}")'
}

http_head() {
  local url="$1"
  curl -i -sS "$url" | sed -n '1,6p'
}

case "$ACTION" in
  start)
    require_docker
    docker compose up -d --build
    ;;
  stop)
    require_docker
    docker compose stop
    docker compose ps
    ;;
  restart)
    require_docker
    docker compose up -d --build
    ;;
  status)
    require_docker
    docker compose ps
    ;;
  health)
    health
    ;;
  refresh-models)
    refresh_models
    ;;
  verify)
    require_docker
    docker compose ps
    printf "\nhealth:\n"
    health
    printf "\nroot status:\n"
    http_head "http://localhost:9000/"
    printf "\nadmin status:\n"
    http_head "http://localhost:9000/admin"
    printf "\nauthenticated model count:\n"
    model_count
    ;;
  logs)
    require_docker
    service="${2:-app}"
    tail_lines="${TAIL:-120}"
    docker compose logs --no-color --tail="$tail_lines" "$service"
    ;;
  *)
    usage
    exit 2
    ;;
esac
