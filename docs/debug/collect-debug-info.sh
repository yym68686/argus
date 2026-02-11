#!/usr/bin/env bash
set -euo pipefail

# Argus debug collector (safe, read-only).
#
# Usage (recommended):
#   export ARGUS_TOKEN="..."   # required for HTTP API calls (will NOT be printed)
#   bash docs/debug/collect-debug-info.sh | tee "/tmp/argus-debug-$(date +%F-%H%M%S).log"
#
# Notes:
# - This script redacts common token patterns from logs/command output.
# - It avoids printing docker inspect fields that contain environment variables.

GATEWAY_HTTP="${ARGUS_GATEWAY_HTTP:-http://127.0.0.1:8080}"
OUTDIR="${OUTDIR:-$(mktemp -d /tmp/argus-debug.XXXXXX)}"
CURL_COMMON_ARGS=(--silent --show-error --location --connect-timeout 3 --max-time 20)

section() {
  echo
  echo "== $* =="
}

have() { command -v "$1" >/dev/null 2>&1; }

redact() {
  # Redact typical secrets in streams:
  # - token=... query params
  # - Authorization: Bearer ...
  # - sk-... style tokens
  # - Telegram bot token style: 123456:ABC...
  python3 - <<'PY'
import re, sys
t = sys.stdin.read()
t = re.sub(r'(token=)[^&\s"]+', r'\1***', t)
t = re.sub(r'(?i)(authorization:\s*bearer\s+)[^\s"]+', r'\1***', t)
t = re.sub(r'\bsk-[A-Za-z0-9]{10,}\b', 'sk-***', t)
t = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}\b', '***:***', t)
sys.stdout.write(t)
PY
}

curl_plain() {
  local url="$1"
  shift || true
  curl "${CURL_COMMON_ARGS[@]}" "$@" "$url"
}

curl_auth() {
  local url="$1"
  shift || true
  if [[ -z "${ARGUS_TOKEN:-}" ]]; then
    echo "(skip) missing ARGUS_TOKEN; cannot call: $url" >&2
    return 2
  fi
  curl "${CURL_COMMON_ARGS[@]}" -H "Authorization: Bearer ${ARGUS_TOKEN}" "$@" "$url"
}

json_pretty() {
  python3 -m json.tool
}

maybe_load_dotenv() {
  if [[ -n "${ARGUS_TOKEN:-}" ]]; then
    return 0
  fi
  if [[ ! -f ".env" ]]; then
    return 0
  fi
  # Parse .env without printing it.
  local tok
  tok="$(
    python3 - <<'PY'
from pathlib import Path
import re
env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    env[k] = v
print(env.get("ARGUS_TOKEN", ""), end="")
PY
  )"
  if [[ -n "$tok" ]]; then
    export ARGUS_TOKEN="$tok"
  fi
}

docker_container_first() {
  local filter="$1"
  docker ps --filter "$filter" --format '{{.Names}}' | head -n 1
}

docker_list_runtime_containers() {
  docker ps --filter "label=io.argus.session_id" --format '{{.Names}}'
}

docker_inspect_summary() {
  local name="$1"
  docker inspect "$name" --format \
    'name={{.Name}} id={{.Id}} status={{.State.Status}} running={{.State.Running}} oom={{.State.OOMKilled}} restart={{.RestartCount}} exit={{.State.ExitCode}} started={{.State.StartedAt}} finished={{.State.FinishedAt}} mem={{.HostConfig.Memory}} memswap={{.HostConfig.MemorySwap}} cpu_quota={{.HostConfig.CpuQuota}} cpu_period={{.HostConfig.CpuPeriod}} pids={{.HostConfig.PidsLimit}}' \
    2>/dev/null || true
}

main() {
  mkdir -p "$OUTDIR"
  maybe_load_dotenv

  section "Meta"
  echo "time_utc=$(date -u +%FT%TZ)"
  echo "host=$(hostname 2>/dev/null || true)"
  echo "kernel=$(uname -a 2>/dev/null || true)"
  echo "outdir=$OUTDIR"
  echo "gateway_http=$GATEWAY_HTTP"
  echo -n "ARGUS_TOKEN bytes="
  if [[ -n "${ARGUS_TOKEN:-}" ]]; then
    printf "%s" "$ARGUS_TOKEN" | wc -c | tr -d " "
    echo
  else
    echo "0 (not set)"
  fi

  section "Host Resources"
  (uptime || true) 2>&1
  (free -m || true) 2>&1
  (df -h || true) 2>&1

  section "Docker Versions"
  (docker version || true) 2>&1
  (docker compose version || true) 2>&1

  section "Containers (docker ps)"
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | redact || true

  section "Argus Containers (heuristics)"
  local gateway bot
  gateway="$(docker_container_first "ancestor=argus-gateway")"
  bot="$(docker_container_first "ancestor=argus-telegram-bot")"
  if [[ -z "$gateway" ]]; then
    gateway="$(docker_container_first "name=gateway")"
  fi
  if [[ -z "$bot" ]]; then
    bot="$(docker_container_first "name=telegram")"
  fi
  echo "gateway_container=${gateway:-<not found>}"
  echo "telegram_bot_container=${bot:-<not found>}"
  echo "runtime_containers:"
  docker_list_runtime_containers || true

  section "docker stats (no stream)"
  (docker stats --no-stream || true) 2>&1 | redact

  section "Gateway HTTP healthz"
  curl_plain "$GATEWAY_HTTP/healthz" | tee "$OUTDIR/healthz.json" | redact || true
  echo

  section "Gateway automation/state (summary)"
  if curl_auth "$GATEWAY_HTTP/automation/state" >"$OUTDIR/automation_state.json"; then
    python3 - <<'PY'
import json
from pathlib import Path
st = json.loads(Path(__import__("os").environ["OUTDIR"] + "/automation_state.json").read_text())
persisted = st.get("persisted") or {}
default_sid = persisted.get("defaultSessionId") or ""
sessions = persisted.get("sessions") or {}
print("defaultSessionId:", default_sid or "<empty>")
print("persisted.sessions count:", len(sessions))
if default_sid and default_sid in sessions:
  sess = sessions[default_sid] or {}
  main_tid = sess.get("mainThreadId") or ""
  print("mainThreadId:", main_tid or "<empty>")
  lanes = (st.get("runtime") or {}).get("lanes") or []
  # show lane for main thread if present
  for lane in lanes:
    if lane.get("sessionId") == default_sid and lane.get("threadId") == main_tid:
      print("lane(main) busy:", lane.get("busy"), "followupDepth:", lane.get("followupDepth"))
  q = (sess.get("systemEventQueues") or {}).get(main_tid) or []
  print("systemEventQueues[main] len:", len(q))
  # last 3 events (kind + meta + createdAt + first line)
  tail = q[-3:]
  for ev in tail:
    kind = ev.get("kind")
    meta = ev.get("meta") or {}
    created = ev.get("createdAtMs")
    text = (ev.get("text") or "").splitlines()[0][:200]
    print("-", kind, "createdAtMs=", created, "meta=", meta, "text[0]=", text)
else:
  print("note: defaultSessionId missing or not found in persisted.sessions")
  lanes = (st.get("runtime") or {}).get("lanes") or []
  print("runtime.lanes count:", len(lanes))
  for lane in lanes[:10]:
    print("-", lane)
PY
  else
    echo "(failed) cannot fetch /automation/state (check ARGUS_TOKEN and gateway reachability)" >&2
  fi

  section "Gateway cron/jobs (summary)"
  if curl_auth "$GATEWAY_HTTP/automation/cron/jobs" >"$OUTDIR/cron_jobs.json"; then
    python3 - <<'PY'
import json
from pathlib import Path
obj = json.loads(Path(__import__("os").environ["OUTDIR"] + "/cron_jobs.json").read_text())
jobs = obj.get("jobs") or []
print("sessionId:", obj.get("sessionId"))
print("jobs count:", len(jobs))
for j in jobs[:15]:
  print("-", j.get("jobId"), "expr=", j.get("expr"), "enabled=", j.get("enabled"), "lastRunAtMs=", j.get("lastRunAtMs"), "nextRunAtMs=", j.get("nextRunAtMs"))
PY
  else
    echo "(failed) cannot fetch /automation/cron/jobs" >&2
  fi

  section "Gateway sessions + nodes"
  if curl_auth "$GATEWAY_HTTP/sessions" >"$OUTDIR/sessions.json"; then
    cat "$OUTDIR/sessions.json" | json_pretty | redact || true
  else
    echo "(failed) cannot fetch /sessions" >&2
  fi
  if curl_auth "$GATEWAY_HTTP/nodes" >"$OUTDIR/nodes.json"; then
    cat "$OUTDIR/nodes.json" | json_pretty | redact || true
  else
    echo "(failed) cannot fetch /nodes" >&2
  fi

  section "Runtime node-host process.list (default session only)"
  if [[ -f "$OUTDIR/automation_state.json" ]] && [[ -f "$OUTDIR/nodes.json" ]]; then
    local sid
    sid="$(
      python3 - <<'PY'
import json, os
st = json.loads(open(os.environ["OUTDIR"] + "/automation_state.json", "r", encoding="utf-8").read())
persisted = st.get("persisted") or {}
print((persisted.get("defaultSessionId") or "").strip(), end="")
PY
    )"
    if [[ -n "$sid" ]]; then
      # Invoke process.list on runtime:<sid>
      curl_auth "$GATEWAY_HTTP/nodes/invoke" \
        -H "Content-Type: application/json" \
        -d "{\"node\":\"runtime:${sid}\",\"command\":\"process.list\",\"params\":{}}" \
        >"$OUTDIR/process_list.json" || true
      if [[ -s "$OUTDIR/process_list.json" ]]; then
        cat "$OUTDIR/process_list.json" | json_pretty | redact || true
      fi
    else
      echo "(skip) defaultSessionId empty; cannot query runtime node-host" >&2
    fi
  fi

  section "Docker inspect summaries (gateway/bot/runtimes)"
  if [[ -n "${gateway:-}" ]]; then
    docker_inspect_summary "$gateway" | redact
  fi
  if [[ -n "${bot:-}" ]]; then
    docker_inspect_summary "$bot" | redact
  fi
  while IFS= read -r rt; do
    [[ -z "$rt" ]] && continue
    docker_inspect_summary "$rt" | redact
  done < <(docker_list_runtime_containers || true)

  section "docker top (runtimes)"
  while IFS= read -r rt; do
    [[ -z "$rt" ]] && continue
    echo "--- docker top $rt ---"
    (docker top "$rt" || true) 2>&1 | redact
  done < <(docker_list_runtime_containers || true)

  section "docker logs tail (gateway/bot/runtimes) [redacted]"
  if [[ -n "${gateway:-}" ]]; then
    echo "--- docker logs --tail 250 $gateway ---"
    (docker logs --tail 250 "$gateway" || true) 2>&1 | redact
  fi
  if [[ -n "${bot:-}" ]]; then
    echo "--- docker logs --tail 250 $bot ---"
    (docker logs --tail 250 "$bot" || true) 2>&1 | redact
  fi
  while IFS= read -r rt; do
    [[ -z "$rt" ]] && continue
    echo "--- docker logs --tail 250 $rt ---"
    (docker logs --tail 250 "$rt" || true) 2>&1 | redact
  done < <(docker_list_runtime_containers || true)

  section "Telegram-bot DNS check (gateway hostname)"
  if [[ -n "${bot:-}" ]]; then
    # Node-based DNS lookup (container should have node).
    (docker exec "$bot" node -e "require('dns').lookup('gateway',(e,a)=>console.log(e?('ERR '+e.message):('OK '+a)))" || true) 2>&1 | redact
  fi

  section "Done"
  echo "Collected files (share if needed):"
  ls -lah "$OUTDIR" || true
}

export OUTDIR
main "$@"
