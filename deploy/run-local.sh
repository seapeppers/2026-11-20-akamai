#!/usr/bin/env bash
# PackServe local stack launcher. Everything is SIMULATED GPU replicas + mock serverless by default.
#
#   deploy/run-local.sh up [--two-gateways] [--replicas N] [--large N] [--no-autoscaler]
#   deploy/run-local.sh down | status | logs [service]
#
# PIDs: .run/<name>.pid   Logs: logs/<name>.log   Replica ids: sim-<port> (autoscaler adopts them).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
RUN_DIR="$ROOT/.run"; LOG_DIR="$ROOT/logs"
export PACKSERVE_RUN_DIR="$RUN_DIR" PACKSERVE_LOG_DIR="$LOG_DIR"

TELEMETRY_PORT=8060; AUTOSCALER_PORT=8070; GATEWAY_PORT=8080; GATEWAY2_PORT=8081
METADATA_PORT=8090; SERVERLESS_PORT=8099
SMALL_MODEL=qwen3-8-27b; LARGE_MODEL=packserve-large-230b
SMALL_PROFILE=${SMALL_PROFILE:-virtual-blackwell-x4}; LARGE_PROFILE=${LARGE_PROFILE:-virtual-blackwell-x8-large}
READY_TIMEOUT=${READY_TIMEOUT:-40}

say() { printf '[run-local] %s\n' "$*"; }

python_bin() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then echo "$ROOT/.venv/bin/python"
  else uv run python -c 'import sys; print(sys.executable)'; fi
}

http_code() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null || true; }

wait_ready() {  # name url
  local name=$1 url=$2 i code
  for ((i = 0; i < READY_TIMEOUT * 2; i++)); do
    code=$(http_code "$url")
    [[ -n "$code" && "$code" != "000" ]] && return 0
    pid_alive "$name" || { say "FAILED: $name exited early; see logs/$name.log"; return 1; }
    sleep 0.5
  done
  say "FAILED: $name not ready at $url after ${READY_TIMEOUT}s; see logs/$name.log"; return 1
}

pid_alive() { local f="$RUN_DIR/$1.pid"; [[ -f $f ]] && kill -0 "$(cat "$f")" 2>/dev/null; }

start() {  # name readiness_url -- cmd...
  local name=$1 url=$2; shift 3
  if pid_alive "$name"; then say "$name already running"; return 0; fi
  nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$RUN_DIR/$name.pid"
  disown 2>/dev/null || true
  wait_ready "$name" "$url" && say "up: $name -> $url"
}

port_listeners() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true; }

cmd_up() {
  local two_gw=0 n_small=2 n_large=1 autoscaler=1
  while (($#)); do
    case $1 in
      --two-gateways) two_gw=1 ;;
      --replicas) n_small=$2; shift ;;
      --large) n_large=$2; shift ;;
      --no-autoscaler) autoscaler=0 ;;
      *) say "unknown option $1"; exit 2 ;;
    esac; shift
  done
  mkdir -p "$RUN_DIR" "$LOG_DIR" "$ROOT/data"
  local PY; PY=$(python_bin)
  local ports=($TELEMETRY_PORT $METADATA_PORT $SERVERLESS_PORT $GATEWAY_PORT $AUTOSCALER_PORT)
  ((two_gw)) && ports+=($GATEWAY2_PORT)
  for p in "${ports[@]}"; do
    if [[ -n "$(port_listeners "$p")" ]] && ! ls "$RUN_DIR"/*.pid >/dev/null 2>&1; then
      say "port $p is already in use by a process not started by run-local; aborting"; exit 1
    fi
  done

  say "SIMULATED stack: replicas are simulated GPUs; overflow goes to ${AKAMAI_BASE_URL:-the mock serverless on :$SERVERLESS_PORT}"
  local rc=0
  start telemetry "http://127.0.0.1:$TELEMETRY_PORT/healthz" -- "$PY" -m packserve.telemetry || rc=1
  start metadata "http://127.0.0.1:$METADATA_PORT/v1/replicas" -- "$PY" -m packserve.metadata || rc=1
  if [[ -z "${AKAMAI_BASE_URL:-}" ]]; then
    start serverless_mock "http://127.0.0.1:$SERVERLESS_PORT/v1/models" -- "$PY" -m packserve.serverless_mock || rc=1
  fi
  ((rc)) && { say "core services failed; run: deploy/run-local.sh down"; exit 1; }

  local port=9001 i name
  spawn_replica() {  # model profile
    name="replica-sim-$port"
    start "$name" "http://127.0.0.1:$port/healthz" -- "$PY" -m packserve.replica_sim \
      --port "$port" --id "sim-$port" --model "$1" --profile "$2" || rc=1
    # .run/replica-sim-<port>.pid is also where the autoscaler's SimProvider looks up adopted replicas
    port=$((port + 1))
  }
  for ((i = 0; i < n_small; i++)); do spawn_replica "$SMALL_MODEL" "$SMALL_PROFILE"; done
  for ((i = 0; i < n_large; i++)); do spawn_replica "$LARGE_MODEL" "$LARGE_PROFILE"; done
  ((rc)) && { say "replicas failed; run: deploy/run-local.sh down"; exit 1; }

  start gateway-gw1 "http://127.0.0.1:$GATEWAY_PORT/v1/models" -- \
    "$PY" -m packserve.gateway --port "$GATEWAY_PORT" --id gw1 || rc=1
  if ((two_gw)); then
    start gateway-gw2 "http://127.0.0.1:$GATEWAY2_PORT/v1/models" -- \
      "$PY" -m packserve.gateway --port "$GATEWAY2_PORT" --id gw2 || rc=1
  fi
  if ((autoscaler)); then
    start autoscaler "http://127.0.0.1:$AUTOSCALER_PORT/healthz" -- \
      "$PY" -m packserve.autoscaler --provider sim \
      --pool "$SMALL_MODEL:$n_small:6:$SMALL_PROFILE" --pool "$LARGE_MODEL:$n_large:2:$LARGE_PROFILE" \
      --pool "gemma-4-26b-a4b-it:0:2:$SMALL_PROFILE" --no-cleanup-on-exit || rc=1
  fi
  ((rc)) && { say "some services failed; run: deploy/run-local.sh down"; exit 1; }
  say "stack is up. Gateway http://127.0.0.1:$GATEWAY_PORT  (demo key: ps_demo_interactive)"
  say "try: curl -si localhost:$GATEWAY_PORT/v1/chat/completions -H 'Authorization: Bearer ps_demo_interactive' -H 'Content-Type: application/json' -d '{\"model\":\"$SMALL_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}' | rg -i x-packserve"
}

kill_pidfile() {
  local f=$1 pid
  pid=$(cat "$f" 2>/dev/null) || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$f"
}

cmd_down() {
  local f
  # autoscaler first so it cannot respawn replicas while we tear down
  [[ -f "$RUN_DIR/autoscaler.pid" ]] && kill_pidfile "$RUN_DIR/autoscaler.pid"
  for f in "$RUN_DIR"/gateway-*.pid; do [[ -e $f ]] && kill_pidfile "$f"; done
  for f in "$RUN_DIR"/*.pid; do [[ -e $f ]] && kill_pidfile "$f"; done
  local bad=0 p pids
  for p in $TELEMETRY_PORT $AUTOSCALER_PORT $GATEWAY_PORT $GATEWAY2_PORT $METADATA_PORT $SERVERLESS_PORT $(seq 9001 9099); do
    pids=$(port_listeners "$p")
    if [[ -n "$pids" ]]; then
      # only report processes that are ours (python from this repo); do not kill strangers
      if ps -o command= -p $pids 2>/dev/null | rg -q 'packserve'; then
        kill $pids 2>/dev/null; sleep 0.5
        [[ -n "$(port_listeners "$p")" ]] && kill -9 $(port_listeners "$p") 2>/dev/null
      fi
      [[ -n "$(port_listeners "$p")" ]] && { say "port $p still in use (not a packserve process?)"; bad=1; }
    fi
  done
  ((bad)) && { say "down: some ports are still busy"; exit 1; }
  say "down: all processes stopped; ports 8060 8070 8080 8081 8090 8099 9001-9099 are free"
}

cmd_status() {
  local f name port code
  shopt -s nullglob
  for f in "$RUN_DIR"/*.pid; do
    name=$(basename "$f" .pid)
    if kill -0 "$(cat "$f")" 2>/dev/null; then printf '%-24s pid %-7s running\n' "$name" "$(cat "$f")"
    else printf '%-24s pid %-7s DEAD\n' "$name" "$(cat "$f")"; fi
  done
  for pair in telemetry:$TELEMETRY_PORT:/healthz metadata:$METADATA_PORT:/v1/replicas gateway:$GATEWAY_PORT:/v1/models \
              autoscaler:$AUTOSCALER_PORT:/healthz serverless:$SERVERLESS_PORT:/v1/models; do
    IFS=: read -r name port path <<<"$pair"
    code=$(http_code "http://127.0.0.1:$port$path")
    printf 'http %-12s :%-5s %s\n' "$name" "$port" "${code:-000}"
  done
  curl -s -m 2 "http://127.0.0.1:$AUTOSCALER_PORT/admin/state" 2>/dev/null | jq -c '{provider,replicas,draining,held_for_billing,last_action}' 2>/dev/null || true
}

cmd_logs() {
  shopt -s nullglob
  if [[ -n "${1:-}" ]]; then tail -n 50 -f "$LOG_DIR/$1.log"; else tail -n 20 -f "$LOG_DIR"/*.log; fi
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  down) cmd_down ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
