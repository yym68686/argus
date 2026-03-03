#!/usr/bin/env bash

set -uo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

TS="$(date -u +%Y%m%dT%H%M%SZ)"
HOST_SHORT="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown-host)"
OUT_DIR="${OUT_DIR:-/tmp/argus_diag_${TS}_${HOST_SHORT}}"

mkdir -p "$OUT_DIR"/{docker,logs,http,checks,files} 2>/dev/null || true

COMPOSE_BIN=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_BIN=(docker-compose)
  else
    echo "ERROR: docker compose is not available (neither 'docker compose' nor 'docker-compose')." >&2
    exit 2
  fi
fi

redact_logs() {
  # Best-effort redaction for shareable logs.
  sed -E \
    -e 's/(token=)[^& ]+/\1***REDACTED***/g' \
    -e 's/(Authorization: Bearer )[A-Za-z0-9._-]+/\1***REDACTED***/gi' \
    -e 's/(sk-)[A-Za-z0-9_-]{10,}/\1***REDACTED***/g' \
    -e 's/argus-(node|mcp|openai)-v1\.[^.]+\.[A-Za-z0-9_-]+/argus-\1-v1.***REDACTED***/g' \
    -e 's/[0-9]{6,}:[A-Za-z0-9_-]{20,}/***REDACTED_TELEGRAM_TOKEN***/g'
}

run_sh() {
  local name="$1"
  shift
  local cmd="$1"
  shift || true
  local out="$OUT_DIR/$name"
  {
    echo "\$ ${cmd}"
    bash -lc "${cmd}"
  } >"$out" 2>&1 || {
    echo "exit=$?" >>"$out"
    return 1
  }
}

run_sh_redacted() {
  local name="$1"
  shift
  local cmd="$1"
  shift || true
  local out="$OUT_DIR/$name"
  {
    echo "\$ ${cmd}"
    bash -lc "${cmd}" 2>&1 | redact_logs
    echo "exit=${PIPESTATUS[0]}"
  } >"$out"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

compose_ps_q() {
  local svc="$1"
  "${COMPOSE_BIN[@]}" ps -q "$svc" 2>/dev/null | tr -d '\r' | head -n 1
}

docker_inspect_one_line() {
  local cid="$1"
  docker inspect -f \
    'name={{.Name}} id={{.Id}} image={{.Config.Image}} status={{.State.Status}} started={{.State.StartedAt}} exit={{.State.ExitCode}}' \
    "$cid" 2>/dev/null || true
}

env_hash_in_container() {
  local cid="$1"
  local var="$2"
  docker exec -i "$cid" sh -lc "python3 - <<'PY'\nimport os, hashlib\nv=os.getenv('$var','')\nprint('set' if v else 'unset')\nprint('len', len(v))\nprint('sha256', hashlib.sha256(v.encode('utf-8')).hexdigest())\nPY" 2>/dev/null || true
}

http_get() {
  local url="$1"
  local token="$2"
  if have_cmd curl; then
    if [ -n "$token" ]; then
      curl --silent --show-error --fail -H "Authorization: Bearer ${token}" "$url"
    else
      curl --silent --show-error --fail "$url"
    fi
    return $?
  fi
  python3 - "$url" "$token" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
token = sys.argv[2]

headers = {}
if token:
  headers["Authorization"] = "Bearer " + token

req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=10) as r:
  sys.stdout.buffer.write(r.read())
PY
}

write_summary() {
  local f="$OUT_DIR/summary.txt"
  {
    echo "Argus diagnostic bundle"
    echo "timestamp_utc=$TS"
    echo "host=$HOST_SHORT"
    echo "cwd=$(pwd)"
    echo
  } >"$f"
}

append_summary() {
  local line="$1"
  echo "$line" >>"$OUT_DIR/summary.txt"
}

write_summary

run_sh docker/uname.txt 'uname -a || true'
run_sh docker/date.txt 'date -u || true'
run_sh docker/uptime.txt 'uptime || true'
run_sh docker/df.txt 'df -h || true'

run_sh docker/git.txt 'git rev-parse --show-toplevel && git rev-parse HEAD && git status -sb || true'

run_sh docker/docker_version.txt 'docker version || true'
run_sh docker/docker_info.txt 'docker info || true'
run_sh docker/compose_version.txt '"${COMPOSE_BIN[@]}" version || true'

run_sh docker/compose_ps.txt '"${COMPOSE_BIN[@]}" ps || true'
run_sh docker/compose_ls.txt '"${COMPOSE_BIN[@]}" ls || true'
run_sh_redacted docker/compose_config.no_interpolate.yml '"${COMPOSE_BIN[@]}" config --no-interpolate || true'

GATEWAY_CID="$(compose_ps_q gateway)"
TG_CID="$(compose_ps_q telegram-bot)"
WEB_CID="$(compose_ps_q web)"

append_summary "gateway_container_id=${GATEWAY_CID:-missing}"
append_summary "telegram_bot_container_id=${TG_CID:-missing}"
append_summary "web_container_id=${WEB_CID:-missing}"

if [ -n "${GATEWAY_CID:-}" ]; then
  docker_inspect_one_line "$GATEWAY_CID" >"$OUT_DIR/docker/gateway.inspect.txt"
  {
    echo "ARGUS_TOKEN"; env_hash_in_container "$GATEWAY_CID" "ARGUS_TOKEN"; echo
    echo "OPENAI_API_KEY"; env_hash_in_container "$GATEWAY_CID" "OPENAI_API_KEY"; echo
    echo "ARGUS_OPENAI_TOKEN"; env_hash_in_container "$GATEWAY_CID" "ARGUS_OPENAI_TOKEN"; echo
    echo "ARGUS_MCP_TOKEN"; env_hash_in_container "$GATEWAY_CID" "ARGUS_MCP_TOKEN"; echo
    echo "ARGUS_NODE_TOKEN"; env_hash_in_container "$GATEWAY_CID" "ARGUS_NODE_TOKEN"; echo
    echo "TELEGRAM_BOT_TOKEN"; env_hash_in_container "$GATEWAY_CID" "TELEGRAM_BOT_TOKEN"; echo
    echo "ARGUS_HOME_HOST_PATH"; env_hash_in_container "$GATEWAY_CID" "ARGUS_HOME_HOST_PATH"; echo
    echo "ARGUS_WORKSPACE_HOST_PATH"; env_hash_in_container "$GATEWAY_CID" "ARGUS_WORKSPACE_HOST_PATH"; echo
    echo "ARGUS_RUNTIME_CMD"; env_hash_in_container "$GATEWAY_CID" "ARGUS_RUNTIME_CMD"; echo
  } >"$OUT_DIR/docker/gateway.env_hashes.txt"
else
  append_summary "gateway_status=not_running"
fi

if [ -n "${TG_CID:-}" ]; then
  docker_inspect_one_line "$TG_CID" >"$OUT_DIR/docker/telegram-bot.inspect.txt"
  {
    echo "ARGUS_TOKEN"; env_hash_in_container "$TG_CID" "ARGUS_TOKEN"; echo
    echo "TELEGRAM_BOT_TOKEN"; env_hash_in_container "$TG_CID" "TELEGRAM_BOT_TOKEN"; echo
    echo "HOST"; env_hash_in_container "$TG_CID" "HOST"; echo
    echo "ARGUS_GATEWAY_WS_URL"; env_hash_in_container "$TG_CID" "ARGUS_GATEWAY_WS_URL"; echo
    echo "ARGUS_GATEWAY_HTTP_URL"; env_hash_in_container "$TG_CID" "ARGUS_GATEWAY_HTTP_URL"; echo
    echo "NEXT_PUBLIC_ARGUS_WS_URL"; env_hash_in_container "$TG_CID" "NEXT_PUBLIC_ARGUS_WS_URL"; echo
    echo "STATE_PATH"; env_hash_in_container "$TG_CID" "STATE_PATH"; echo
  } >"$OUT_DIR/docker/telegram-bot.env_hashes.txt"
else
  append_summary "telegram_bot_status=not_running"
fi

# Runtime containers spawned by the gateway
run_sh docker/runtime_containers.txt 'docker ps -a --filter "label=io.argus.gateway=apps/api" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}" || true'
mapfile -t RUNTIME_CIDS < <(docker ps -aq --filter "label=io.argus.gateway=apps/api" 2>/dev/null || true)
if [ "${#RUNTIME_CIDS[@]}" -gt 0 ]; then
  append_summary "runtime_container_count=${#RUNTIME_CIDS[@]}"
else
  append_summary "runtime_container_count=0"
fi

for cid in "${RUNTIME_CIDS[@]:0:12}"; do
  name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's#^/##' || echo "$cid")"
  docker_inspect_one_line "$cid" >"$OUT_DIR/docker/runtime_${name}.inspect.txt"
  docker logs --tail 400 "$cid" 2>&1 | redact_logs >"$OUT_DIR/logs/runtime_${name}.log" || true
done

# Service logs (redacted)
run_sh_redacted logs/gateway.log '"${COMPOSE_BIN[@]}" logs --no-color --tail 2000 gateway || "${COMPOSE_BIN[@]}" logs --tail 2000 gateway || true'
run_sh_redacted logs/telegram-bot.log '"${COMPOSE_BIN[@]}" logs --no-color --tail 2000 telegram-bot || "${COMPOSE_BIN[@]}" logs --tail 2000 telegram-bot || true'
run_sh_redacted logs/web.log '"${COMPOSE_BIN[@]}" logs --no-color --tail 1000 web || "${COMPOSE_BIN[@]}" logs --tail 1000 web || true'

# Capture current Codex config(s) (no secrets expected, but keep in files/).
run_sh files/codex_config.toml 'set -euo pipefail; shopt -s nullglob; home="${ARGUS_HOME_HOST_PATH:-$HOME/.argus}"; base="${ARGUS_WORKSPACE_HOST_PATH:-}"; echo "home=$home"; if [ -n "$base" ]; then echo "workspace_base=$base"; fi; echo; files=(); for f in "$home/.codex/config.toml" "$home/workspace/.codex/config.toml" "$home"/workspace-*/.codex/config.toml "$home"/workspaces/sess-*/.codex/config.toml; do if [ -f "$f" ]; then files+=("$f"); fi; done; if [ -n "$base" ]; then for f in "$base"/sess-*/.codex/config.toml; do if [ -f "$f" ]; then files+=("$f"); fi; done; fi; if [ "${#files[@]}" -eq 0 ]; then echo "(no Codex config.toml found)"; exit 0; fi; i=0; for f in "${files[@]}"; do i=$((i+1)); if [ "$i" -gt 20 ]; then echo "(truncated; showing first 20)"; break; fi; echo "----- $f"; sed -n "1,200p" "$f"; echo; done'

# Gateway HTTP checks from the host (best-effort).
HOST_GATEWAY_URL="${HOST_GATEWAY_URL:-http://127.0.0.1:8080}"
ARGUS_TOKEN_VALUE=""
if [ -n "${GATEWAY_CID:-}" ]; then
  ARGUS_TOKEN_VALUE="$(docker exec -i "$GATEWAY_CID" sh -lc 'printf "%s" "${ARGUS_TOKEN:-}"' 2>/dev/null || true)"
fi

{
  echo "HOST_GATEWAY_URL=$HOST_GATEWAY_URL"
  echo "healthz:"
  http_get "${HOST_GATEWAY_URL}/healthz" "" || true
} 2>&1 | redact_logs >"$OUT_DIR/http/healthz.txt"

{
  echo "sessions:"
  http_get "${HOST_GATEWAY_URL}/sessions" "$ARGUS_TOKEN_VALUE" || true
} 2>&1 | redact_logs >"$OUT_DIR/http/sessions.json"

{
  echo "nodes:"
  http_get "${HOST_GATEWAY_URL}/nodes" "$ARGUS_TOKEN_VALUE" || true
} 2>&1 | redact_logs >"$OUT_DIR/http/nodes.json"

{
  echo "automation/state:"
  http_get "${HOST_GATEWAY_URL}/automation/state" "$ARGUS_TOKEN_VALUE" || true
} 2>&1 | redact_logs >"$OUT_DIR/http/automation_state.json"

# Telegram-bot container connectivity checks to gateway.
if [ -n "${TG_CID:-}" ]; then
  # Non-secret env values (redacted for shareability).
  docker exec -i "$TG_CID" sh -lc '
echo "HOST=${HOST:-}"
echo "ARGUS_GATEWAY_HTTP_URL=${ARGUS_GATEWAY_HTTP_URL:-}"
echo "ARGUS_GATEWAY_WS_URL=${ARGUS_GATEWAY_WS_URL:-}"
echo "NEXT_PUBLIC_ARGUS_WS_URL=${NEXT_PUBLIC_ARGUS_WS_URL:-}"
echo "ARGUS_CWD=${ARGUS_CWD:-}"
echo "STATE_PATH=${STATE_PATH:-}"
' 2>&1 | redact_logs >"$OUT_DIR/files/telegram-bot.env_sanitized.txt" || true

  # Telegram API reachability (avoid getUpdates to not interfere with polling).
  docker exec -i "$TG_CID" node - <<'NODE' 2>&1 | redact_logs >"$OUT_DIR/checks/telegram-bot.telegram_api_check.txt" || true
const token = (process.env.TELEGRAM_BOT_TOKEN || "").trim();
if (!token) {
  console.log("TELEGRAM_BOT_TOKEN: (unset)");
  process.exit(0);
}

async function call(method) {
  const url = `https://api.telegram.org/bot${token}/${method}`;
  const r = await fetch(url, { method: "GET" });
  const text = await r.text();
  console.log(`GET ${method}:`, r.status);
  console.log("body_prefix:", text.slice(0, 500));
}

async function main() {
  await call("getMe");
  await call("getWebhookInfo");
}

main().catch((e) => {
  console.error("fatal:", e && e.message ? e.message : String(e));
  process.exit(1);
});
NODE

  docker exec -i "$TG_CID" node - <<'NODE' >"$OUT_DIR/checks/telegram-bot.gateway_check.txt" 2>&1 || true
const process = require("node:process");
const WebSocket = require("ws");

function stripOuterQuotes(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function stripSessionFromWsUrl(wsUrl) {
  const raw = stripOuterQuotes(wsUrl);
  if (!raw) return null;
  try {
    const u = new URL(raw);
    u.searchParams.delete("session");
    u.hash = "";
    return u.toString();
  } catch {
    return raw;
  }
}

function deriveHttpBaseFromWsUrl(wsUrl) {
  const raw = stripOuterQuotes(wsUrl);
  if (!raw) return null;
  try {
    const u = new URL(raw);
    u.protocol = u.protocol === "wss:" ? "https:" : "http:";
    u.pathname = "";
    u.search = "";
    u.hash = "";
    return u.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

function deriveGatewayFromHostEnv(hostEnv, { runningInDocker = false } = {}) {
  let raw = stripOuterQuotes(hostEnv);
  if (!raw) raw = runningInDocker ? "gateway" : "127.0.0.1";
  if (raw.startsWith("http://") || raw.startsWith("https://")) {
    const u = new URL(raw);
    const httpBase = `${u.protocol}//${u.host}`;
    const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
    const wsBase = `${wsProto}//${u.host}/ws`;
    return { httpBase, wsBase };
  }
  if (raw.startsWith("ws://") || raw.startsWith("wss://")) {
    const ws = new URL(raw);
    const wsBase = stripSessionFromWsUrl(ws.toString()) || ws.toString();
    const httpProto = ws.protocol === "wss:" ? "https:" : "http:";
    const httpBase = `${httpProto}//${ws.host}`;
    return { httpBase, wsBase };
  }
  if (runningInDocker) {
    if (raw === "127.0.0.1" || raw === "localhost") raw = "gateway";
    if (raw.startsWith("127.0.0.1:")) raw = `gateway:${raw.slice("127.0.0.1:".length)}`;
    if (raw.startsWith("localhost:")) raw = `gateway:${raw.slice("localhost:".length)}`;
  }
  const hostPort = raw.includes(":") ? raw : `${raw}:8080`;
  return { httpBase: `http://${hostPort}`, wsBase: `ws://${hostPort}/ws` };
}

async function main() {
  const runningInDocker = true;
  const gatewayWsUrlRaw = stripSessionFromWsUrl(process.env.ARGUS_GATEWAY_WS_URL) || stripSessionFromWsUrl(process.env.NEXT_PUBLIC_ARGUS_WS_URL);
  const gatewayHttpUrlRaw = stripOuterQuotes(process.env.ARGUS_GATEWAY_HTTP_URL);
  const fromHost = deriveGatewayFromHostEnv(process.env.HOST, { runningInDocker });
  const gatewayWsUrl = gatewayWsUrlRaw || fromHost.wsBase;
  const gatewayHttpUrl = gatewayHttpUrlRaw || deriveHttpBaseFromWsUrl(gatewayWsUrl) || fromHost.httpBase;

  const token = (process.env.ARGUS_TOKEN || "").trim();
  const wsUrl = (() => {
    const u = new URL(gatewayWsUrl);
    if (token) u.searchParams.set("token", token);
    // Use a non-existent session so we don't create new sessions during diagnostics.
    u.searchParams.set("session", "diag-do-not-create");
    return u.toString();
  })();

  console.log("env.HOST:", process.env.HOST || "(unset)");
  console.log("derived.gatewayHttpUrl:", gatewayHttpUrl);
  console.log("derived.gatewayWsUrl:", gatewayWsUrl);

  // HTTP checks
  const hdrs = token ? { authorization: `Bearer ${token}` } : {};
  try {
    const r = await fetch(`${gatewayHttpUrl}/healthz`, { headers: hdrs });
    console.log("GET /healthz:", r.status);
    console.log("body:", await r.text());
  } catch (e) {
    console.log("GET /healthz failed:", e && e.message ? e.message : String(e));
  }
  try {
    const r = await fetch(`${gatewayHttpUrl}/automation/state`, { headers: hdrs });
    console.log("GET /automation/state:", r.status);
    console.log("body_prefix:", (await r.text()).slice(0, 400));
  } catch (e) {
    console.log("GET /automation/state failed:", e && e.message ? e.message : String(e));
  }

  // WS check
  await new Promise((resolve) => {
    const ws = new WebSocket(wsUrl);
    const t = setTimeout(() => {
      console.log("WS timeout");
      try { ws.close(); } catch {}
      resolve();
    }, 8000);
    ws.on("open", () => console.log("WS open"));
    ws.on("message", (data) => console.log("WS message:", String(data).slice(0, 200)));
    ws.on("close", (code, reason) => {
      clearTimeout(t);
      console.log("WS close:", code, reason ? String(reason) : "");
      resolve();
    });
    ws.on("error", (err) => {
      clearTimeout(t);
      console.log("WS error:", err && err.message ? err.message : String(err));
      resolve();
    });
  });
}
main().catch((e) => {
  console.error("fatal:", e && e.message ? e.message : String(e));
  process.exit(1);
});
NODE
fi

# Pack into a tarball for easy sharing.
TARBALL="${OUT_DIR}.tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")" 2>/dev/null || true

{
  echo
  echo "Wrote diagnostic bundle:"
  echo "  $OUT_DIR"
  echo "Tarball (if created):"
  echo "  $TARBALL"
  echo
  echo "Share these files:"
  echo "  $OUT_DIR/summary.txt"
  echo "  $OUT_DIR/logs/gateway.log"
  echo "  $OUT_DIR/logs/telegram-bot.log"
  echo "  $OUT_DIR/checks/telegram-bot.gateway_check.txt"
  echo "  $OUT_DIR/http/automation_state.json"
  echo "  $OUT_DIR/http/sessions.json"
} | tee "$OUT_DIR/README.txt" >/dev/null
