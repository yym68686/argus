import os from "node:os";
import process from "node:process";
import { spawn } from "node:child_process";
import fs from "node:fs";
import WebSocket from "ws";

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      out[key] = true;
      continue;
    }
    out[key] = next;
    i++;
  }
  return out;
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function coerceStringArray(value) {
  if (!Array.isArray(value)) return null;
  const out = [];
  for (const v of value) {
    if (typeof v !== "string") return null;
    out.push(v);
  }
  return out;
}

async function runCommand(params) {
  const argv = coerceStringArray(params?.argv);
  if (!argv || argv.length === 0) {
    return { ok: false, error: { code: "BAD_INPUT", message: "system.run requires argv: string[]" } };
  }
  const cwd = typeof params?.cwd === "string" && params.cwd.trim() ? params.cwd : undefined;
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? Number(params.timeoutMs) : undefined;
  const env =
    params?.env && typeof params.env === "object" && !Array.isArray(params.env)
      ? Object.fromEntries(
          Object.entries(params.env).filter(
            ([k, v]) => typeof k === "string" && typeof v === "string",
          ),
        )
      : undefined;

  return await new Promise((resolve) => {
    const child = spawn(argv[0], argv.slice(1), {
      cwd,
      env: env ? { ...process.env, ...env } : process.env,
      shell: false,
    });

    let stdout = "";
    let stderr = "";
    let killedByTimeout = false;
    const maxBytes = 2 * 1024 * 1024;

    const onChunk = (isStdout, chunk) => {
      const s = chunk.toString("utf8");
      if (isStdout) stdout += s;
      else stderr += s;
      if (stdout.length + stderr.length > maxBytes) {
        try {
          child.kill("SIGKILL");
        } catch {
          // ignore
        }
      }
    };

    child.stdout?.on("data", (c) => onChunk(true, c));
    child.stderr?.on("data", (c) => onChunk(false, c));

    let timer = null;
    if (timeoutMs && timeoutMs > 0) {
      timer = setTimeout(() => {
        killedByTimeout = true;
        try {
          child.kill("SIGKILL");
        } catch {
          // ignore
        }
      }, timeoutMs);
    }

    child.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({
        ok: false,
        error: { code: "SPAWN_FAILED", message: String(err?.message || err) },
      });
    });

    child.on("close", (code, signal) => {
      if (timer) clearTimeout(timer);
      resolve({
        ok: true,
        payload: {
          argv,
          cwd: cwd ?? null,
          timeoutMs: timeoutMs ?? null,
          exitCode: typeof code === "number" ? code : null,
          signal: signal ?? null,
          timedOut: killedByTimeout,
          stdout,
          stderr,
        },
      });
    });
  });
}

function which(params) {
  const bin = typeof params?.bin === "string" ? params.bin.trim() : "";
  if (!bin) return { ok: false, error: { code: "BAD_INPUT", message: "system.which requires bin" } };
  const pathEnv = process.env.PATH || "";
  const parts = pathEnv.split(":");
  for (const p of parts) {
    if (!p) continue;
    const full = `${p}/${bin}`;
    try {
      const st = fs.statSync(full);
      if (st.isFile()) return { ok: true, payload: { bin, path: full } };
    } catch {
      // ignore
    }
  }
  return { ok: true, payload: { bin, path: null } };
}

async function handleInvoke(frame) {
  const cmd = String(frame.command || "");
  const params = typeof frame.paramsJSON === "string" ? parseJson(frame.paramsJSON) : frame.params;
  if (cmd === "system.run") return await runCommand(params);
  if (cmd === "system.which") return which(params);
  return { ok: false, error: { code: "UNSUPPORTED", message: `unsupported command: ${cmd}` } };
}

async function run() {
  const args = parseArgs(process.argv);
  const url =
    (typeof args.url === "string" && args.url.trim()) ||
    (process.env.ARGUS_NODE_WS_URL || "").trim();
  if (!url) {
    // eslint-disable-next-line no-console
    console.error("Missing --url (or ARGUS_NODE_WS_URL). Example: ws://127.0.0.1:8080/nodes/ws?token=...");
    process.exit(2);
  }

  const nodeId =
    (typeof args["node-id"] === "string" && args["node-id"].trim()) ||
    process.env.ARGUS_NODE_ID ||
    os.hostname();
  const displayName =
    (typeof args["display-name"] === "string" && args["display-name"].trim()) ||
    process.env.ARGUS_NODE_DISPLAY_NAME ||
    os.hostname();

  const caps = ["system"];
  const commands = ["system.run", "system.which"];

  const reconnectDelayMs = 1000;

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const ws = new WebSocket(url);

    await new Promise((resolve) => ws.once("open", resolve));
    ws.send(
      JSON.stringify({
        type: "connect",
        nodeId,
        displayName,
        platform: process.platform,
        version: "0.1.0",
        caps,
        commands,
      }),
    );

    const heartbeat = setInterval(() => {
      try {
        ws.send(JSON.stringify({ type: "event", event: "node.heartbeat", payload: { t: Date.now() } }));
      } catch {
        // ignore
      }
    }, 15000);

    const closed = await new Promise((resolve) => {
      ws.on("message", async (data) => {
        const msg = parseJson(String(data));
        if (!msg || typeof msg !== "object") return;
        if (msg.type !== "event" || msg.event !== "node.invoke.request") return;
        const payload = msg.payload && typeof msg.payload === "object" ? msg.payload : null;
        if (!payload) return;
        const id = String(payload.id || "");
        if (!id) return;

        const result = await handleInvoke(payload);
        const reply = {
          type: "event",
          event: "node.invoke.result",
          payload: {
            id,
            nodeId,
            ok: Boolean(result.ok),
            payload: result.payload ?? undefined,
            payloadJSON: result.payload ? JSON.stringify(result.payload) : null,
            error: result.error ?? null,
          },
        };
        try {
          ws.send(JSON.stringify(reply));
        } catch {
          // ignore
        }
      });
      ws.on("close", () => resolve(true));
      ws.on("error", () => resolve(true));
    });

    clearInterval(heartbeat);
    if (!closed) return;
    await new Promise((r) => setTimeout(r, reconnectDelayMs));
  }
}

run().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err);
  process.exit(1);
});
