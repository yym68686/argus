import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import process from "node:process";
import { spawn } from "node:child_process";
import fs from "node:fs";
import WebSocket from "ws";

const DEFAULT_YIELD_MS = 10_000;
const DEFAULT_LOG_TAIL_BYTES = 2 * 1024 * 1024;
const DEFAULT_LOGS_TAIL_BYTES = 64 * 1024;
const MAX_COMPLETED_JOBS = 200;
const JOB_DIRNAME = "jobs";

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

function clampInt(value, { min, max }) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const i = Math.trunc(n);
  return Math.max(min, Math.min(max, i));
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function safeBasename(s) {
  const raw = String(s || "").trim() || "node";
  return raw.replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 120) || "node";
}

function resolveStateDir({ nodeId }) {
  const explicit = (process.env.ARGUS_NODE_STATE_DIR || "").trim();
  if (explicit) return explicit;

  const appHome = (process.env.APP_HOME || "").trim();
  if (appHome) return path.join(appHome, "node-host", safeBasename(nodeId));

  const home = (process.env.HOME || os.homedir() || "").trim() || ".";
  return path.join(home, ".argus", "node-host", safeBasename(nodeId));
}

function readJsonFile(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function writeJsonFile(filePath, data) {
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + "\n");
}

class TailBuffer {
  constructor(capBytes) {
    this.capBytes = capBytes;
    this.chunks = [];
    this.size = 0;
  }

  push(buf) {
    if (!buf || buf.length === 0) return;
    this.chunks.push(buf);
    this.size += buf.length;
    while (this.size > this.capBytes) {
      const overflow = this.size - this.capBytes;
      const first = this.chunks[0];
      if (overflow >= first.length) {
        this.chunks.shift();
        this.size -= first.length;
      } else {
        this.chunks[0] = first.subarray(overflow);
        this.size -= overflow;
      }
    }
  }

  toBuffer() {
    return Buffer.concat(this.chunks, this.size);
  }

  toStringUtf8() {
    return this.toBuffer().toString("utf8");
  }
}

class JobStore {
  constructor({ stateDir, nodeId, sendEvent }) {
    this.stateDir = stateDir;
    this.jobsDir = path.join(stateDir, JOB_DIRNAME);
    this.nodeId = nodeId;
    this.sendEvent = sendEvent;
    this.jobs = new Map();

    fs.mkdirSync(this.jobsDir, { recursive: true });
    this._loadFromDisk();
    this._cleanupCompleted();
  }

  _jobPaths(jobId) {
    const dir = path.join(this.jobsDir, jobId);
    return {
      dir,
      meta: path.join(dir, "meta.json"),
      stdout: path.join(dir, "stdout.log"),
      stderr: path.join(dir, "stderr.log"),
    };
  }

  _loadFromDisk() {
    let entries = [];
    try {
      entries = fs.readdirSync(this.jobsDir, { withFileTypes: true });
    } catch {
      entries = [];
    }
    for (const ent of entries) {
      if (!ent.isDirectory()) continue;
      const jobId = ent.name;
      const p = this._jobPaths(jobId);
      const meta = readJsonFile(p.meta);
      if (!meta || typeof meta !== "object") continue;
      // Best-effort: a restarted node-host can't reattach to running jobs.
      if (meta.running) {
        meta.running = false;
        meta.orphaned = true;
        try {
          writeJsonFile(p.meta, meta);
        } catch {
          // ignore
        }
      }
      this.jobs.set(jobId, { meta, runtime: null });
    }
  }

  _cleanupCompleted() {
    const completed = [];
    for (const [jobId, rec] of this.jobs.entries()) {
      const endedAtMs = Number(rec?.meta?.endedAtMs);
      if (!endedAtMs || rec?.meta?.running) continue;
      completed.push({ jobId, endedAtMs });
    }
    completed.sort((a, b) => b.endedAtMs - a.endedAtMs);
    const keep = new Set(completed.slice(0, MAX_COMPLETED_JOBS).map((x) => x.jobId));
    for (const { jobId } of completed.slice(MAX_COMPLETED_JOBS)) {
      if (keep.has(jobId)) continue;
      this._deleteJob(jobId);
    }
  }

  _deleteJob(jobId) {
    const p = this._jobPaths(jobId);
    try {
      fs.rmSync(p.dir, { recursive: true, force: true });
    } catch {
      // ignore
    }
    this.jobs.delete(jobId);
  }

  listJobs() {
    const out = [];
    for (const [jobId, rec] of this.jobs.entries()) {
      const m = rec.meta || {};
      out.push({
        jobId,
        running: Boolean(m.running),
        orphaned: Boolean(m.orphaned),
        pid: Number.isFinite(m.pid) ? m.pid : null,
        argv: Array.isArray(m.argv) ? m.argv : null,
        cwd: typeof m.cwd === "string" ? m.cwd : null,
        createdAtMs: Number.isFinite(m.createdAtMs) ? m.createdAtMs : null,
        startedAtMs: Number.isFinite(m.startedAtMs) ? m.startedAtMs : null,
        endedAtMs: Number.isFinite(m.endedAtMs) ? m.endedAtMs : null,
        exitCode: Number.isFinite(m.exitCode) ? m.exitCode : null,
        signal: typeof m.signal === "string" ? m.signal : null,
        timedOut: Boolean(m.timedOut),
      });
    }
    out.sort((a, b) => {
      const ar = a.running ? 0 : 1;
      const br = b.running ? 0 : 1;
      if (ar !== br) return ar - br;
      return (b.startedAtMs || b.createdAtMs || 0) - (a.startedAtMs || a.createdAtMs || 0);
    });
    return out;
  }

  getJob(jobId) {
    const rec = this.jobs.get(jobId);
    if (!rec) return null;
    return rec.meta || null;
  }

  getLogs(jobId, { tailBytes }) {
    const rec = this.jobs.get(jobId);
    if (!rec) return null;
    const runtime = rec.runtime;
    const tb = clampInt(tailBytes, { min: 1024, max: 2 * 1024 * 1024 }) || DEFAULT_LOGS_TAIL_BYTES;

    if (runtime && runtime.running) {
      const stdoutBuf = runtime.stdoutTail.toBuffer();
      const stderrBuf = runtime.stderrTail.toBuffer();
      return {
        jobId,
        running: true,
        stdout: stdoutBuf.subarray(Math.max(0, stdoutBuf.length - tb)).toString("utf8"),
        stderr: stderrBuf.subarray(Math.max(0, stderrBuf.length - tb)).toString("utf8"),
        stdoutBytes: runtime.stdoutBytes,
        stderrBytes: runtime.stderrBytes,
        stdoutTruncated: runtime.stdoutBytes > runtime.stdoutTail.capBytes,
        stderrTruncated: runtime.stderrBytes > runtime.stderrTail.capBytes,
      };
    }

    const p = this._jobPaths(jobId);
    const stdout = this._readTailUtf8(p.stdout, tb);
    const stderr = this._readTailUtf8(p.stderr, tb);
    const meta = rec.meta || {};
    return {
      jobId,
      running: false,
      stdout,
      stderr,
      stdoutBytes: Number.isFinite(meta.stdoutBytes) ? meta.stdoutBytes : null,
      stderrBytes: Number.isFinite(meta.stderrBytes) ? meta.stderrBytes : null,
      stdoutTruncated: Boolean(meta.stdoutTruncated),
      stderrTruncated: Boolean(meta.stderrTruncated),
    };
  }

  _readTailUtf8(filePath, tailBytes) {
    try {
      const st = fs.statSync(filePath);
      const size = st.size;
      const start = Math.max(0, size - tailBytes);
      const fd = fs.openSync(filePath, "r");
      try {
        const buf = Buffer.alloc(size - start);
        fs.readSync(fd, buf, 0, buf.length, start);
        return buf.toString("utf8");
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      return "";
    }
  }

  async kill(jobId) {
    const rec = this.jobs.get(jobId);
    if (!rec) return { ok: false, error: { code: "NOT_FOUND", message: `unknown jobId: ${jobId}` } };
    if (!rec.runtime || !rec.runtime.running || !rec.runtime.child) {
      return { ok: false, error: { code: "NOT_RUNNING", message: `job not running: ${jobId}` } };
    }
    const child = rec.runtime.child;
    try {
      child.kill("SIGTERM");
    } catch {
      // ignore
    }
    await sleep(1500);
    if (rec.runtime.running) {
      try {
        child.kill("SIGKILL");
      } catch {
        // ignore
      }
    }
    return { ok: true, payload: { jobId, signal: "SIGTERM" } };
  }

	  async run({ argv, cwd, env, timeoutMs, yieldMs, notifyOnExit }) {
	    const jobId = crypto.randomUUID();
	    const p = this._jobPaths(jobId);
	    fs.mkdirSync(p.dir, { recursive: true });

	    const createdAtMs = Date.now();
	    const notifyMode = notifyOnExit === true ? "always" : notifyOnExit === false ? "never" : "auto";
	    const meta = {
	      version: 1,
	      jobId,
	      nodeId: this.nodeId,
      argv,
      cwd: cwd ?? null,
      createdAtMs,
      startedAtMs: null,
      endedAtMs: null,
      pid: null,
      running: true,
	      orphaned: false,
	      timeoutMs: timeoutMs ?? null,
	      yieldMs: yieldMs ?? null,
	      notifyOnExit: notifyMode === "always",
	      notifyOnExitMode: notifyMode,
	      exitCode: null,
	      signal: null,
	      timedOut: false,
	      stdoutBytes: 0,
      stderrBytes: 0,
      stdoutTruncated: false,
      stderrTruncated: false,
    };

    writeJsonFile(p.meta, meta);

    const stdoutTail = new TailBuffer(DEFAULT_LOG_TAIL_BYTES);
    const stderrTail = new TailBuffer(DEFAULT_LOG_TAIL_BYTES);

    const child = spawn(argv[0], argv.slice(1), {
      cwd: cwd ?? undefined,
      env: env ? { ...process.env, ...env } : process.env,
      shell: false,
    });

    meta.pid = child.pid ?? null;
    meta.startedAtMs = Date.now();
    writeJsonFile(p.meta, meta);

    const runtime = {
      running: true,
      child,
      stdoutTail,
      stderrTail,
      stdoutBytes: 0,
      stderrBytes: 0,
      timeoutTimer: null,
    };
    this.jobs.set(jobId, { meta, runtime });

    if (timeoutMs && timeoutMs > 0) {
      runtime.timeoutTimer = setTimeout(() => {
        meta.timedOut = true;
        try {
          child.kill("SIGKILL");
        } catch {
          // ignore
        }
      }, timeoutMs);
    }

    child.stdout?.on("data", (c) => {
      const b = Buffer.isBuffer(c) ? c : Buffer.from(String(c), "utf8");
      runtime.stdoutBytes += b.length;
      stdoutTail.push(b);
    });
    child.stderr?.on("data", (c) => {
      const b = Buffer.isBuffer(c) ? c : Buffer.from(String(c), "utf8");
      runtime.stderrBytes += b.length;
      stderrTail.push(b);
    });

    const done = new Promise((resolve) => {
      child.on("error", (err) => {
        runtime.running = false;
        if (runtime.timeoutTimer) clearTimeout(runtime.timeoutTimer);
        meta.running = false;
        meta.endedAtMs = Date.now();
        meta.exitCode = null;
        meta.signal = null;
        meta.stdoutBytes = runtime.stdoutBytes;
        meta.stderrBytes = runtime.stderrBytes;
        meta.stdoutTruncated = runtime.stdoutBytes > stdoutTail.capBytes;
        meta.stderrTruncated = runtime.stderrBytes > stderrTail.capBytes;
        try {
          writeJsonFile(p.meta, meta);
        } catch {
          // ignore
        }
        resolve({ kind: "error", error: err });
      });

      child.on("close", (code, signal) => {
        runtime.running = false;
        if (runtime.timeoutTimer) clearTimeout(runtime.timeoutTimer);

        meta.running = false;
        meta.endedAtMs = Date.now();
        meta.exitCode = typeof code === "number" ? code : null;
        meta.signal = signal ?? null;
        meta.stdoutBytes = runtime.stdoutBytes;
        meta.stderrBytes = runtime.stderrBytes;
        meta.stdoutTruncated = runtime.stdoutBytes > stdoutTail.capBytes;
        meta.stderrTruncated = runtime.stderrBytes > stderrTail.capBytes;

        try {
          writeJsonFile(p.meta, meta);
          fs.writeFileSync(p.stdout, stdoutTail.toBuffer());
          fs.writeFileSync(p.stderr, stderrTail.toBuffer());
        } catch {
          // ignore
        }

        if (meta.notifyOnExit) {
          const stdoutPreview = stdoutTail.toStringUtf8().slice(-4000);
          const stderrPreview = stderrTail.toStringUtf8().slice(-4000);
          this.sendEvent("node.process.exited", {
            nodeId: this.nodeId,
            sessionId: (process.env.ARGUS_SESSION_ID || "").trim() || null,
            jobId,
            argv,
            cwd: cwd ?? null,
            exitCode: meta.exitCode,
            signal: meta.signal,
            timedOut: Boolean(meta.timedOut),
            startedAtMs: meta.startedAtMs,
            endedAtMs: meta.endedAtMs,
            stdoutTail: stdoutPreview,
            stderrTail: stderrPreview,
          });
        }

        this._cleanupCompleted();
        resolve({ kind: "close", exitCode: meta.exitCode, signal: meta.signal });
      });
    });

	    const ym = clampInt(yieldMs, { min: 0, max: 60 * 60 * 1000 }) ?? DEFAULT_YIELD_MS;
	    if (ym === 0) {
	      if (notifyMode === "auto" && !meta.notifyOnExit) {
	        meta.notifyOnExit = true;
	        try {
	          writeJsonFile(p.meta, meta);
	        } catch {
	          // ignore
	        }
	      }
	      return { ok: true, payload: this._runningPayload(jobId) };
	    }

    const outcome = await Promise.race([done, sleep(ym).then(() => null)]);

    if (outcome && outcome.kind === "error") {
      return {
        ok: false,
        error: { code: "SPAWN_FAILED", message: String(outcome.error?.message || outcome.error) },
      };
    }

	    if (outcome && outcome.kind === "close") {
	      const rec = this.jobs.get(jobId);
	      const m = rec?.meta || meta;
      const stdout = stdoutTail.toStringUtf8();
      const stderr = stderrTail.toStringUtf8();
      return {
        ok: true,
        payload: {
          jobId,
          running: false,
          argv,
          cwd: cwd ?? null,
          timeoutMs: timeoutMs ?? null,
          yieldMs: ym,
          pid: meta.pid,
          exitCode: m.exitCode ?? null,
          signal: m.signal ?? null,
          timedOut: Boolean(m.timedOut),
          stdout,
          stderr,
          stdoutBytes: m.stdoutBytes ?? runtime.stdoutBytes,
          stderrBytes: m.stderrBytes ?? runtime.stderrBytes,
          stdoutTruncated: Boolean(m.stdoutTruncated),
          stderrTruncated: Boolean(m.stderrTruncated),
        },
      };
	    }

	    if (notifyMode === "auto" && !meta.notifyOnExit) {
	      meta.notifyOnExit = true;
	      try {
	        writeJsonFile(p.meta, meta);
	      } catch {
	        // ignore
	      }
	    }
	    return { ok: true, payload: this._runningPayload(jobId) };
	  }

  _runningPayload(jobId) {
    const rec = this.jobs.get(jobId);
    const meta = rec?.meta || {};
    const runtime = rec?.runtime;
    const stdoutTail = runtime?.stdoutTail?.toStringUtf8?.() ?? "";
    const stderrTail = runtime?.stderrTail?.toStringUtf8?.() ?? "";
    return {
      jobId,
      running: true,
      argv: Array.isArray(meta.argv) ? meta.argv : null,
      cwd: typeof meta.cwd === "string" ? meta.cwd : null,
      timeoutMs: Number.isFinite(meta.timeoutMs) ? meta.timeoutMs : null,
      yieldMs: Number.isFinite(meta.yieldMs) ? meta.yieldMs : null,
      pid: Number.isFinite(meta.pid) ? meta.pid : null,
      stdoutTail: stdoutTail.slice(-4000),
      stderrTail: stderrTail.slice(-4000),
      stdoutBytes: runtime ? runtime.stdoutBytes : Number.isFinite(meta.stdoutBytes) ? meta.stdoutBytes : null,
      stderrBytes: runtime ? runtime.stderrBytes : Number.isFinite(meta.stderrBytes) ? meta.stderrBytes : null,
    };
  }
}

let STORE = null;
let SEND_EVENT = null;

async function runCommand(params) {
  const argv = coerceStringArray(params?.argv);
  if (!argv || argv.length === 0) {
    return { ok: false, error: { code: "BAD_INPUT", message: "system.run requires argv: string[]" } };
  }
  const cwd = typeof params?.cwd === "string" && params.cwd.trim() ? params.cwd : undefined;
  const timeoutMs = Number.isFinite(params?.timeoutMs) ? Number(params.timeoutMs) : undefined;
  const yieldMs = Number.isFinite(params?.yieldMs) ? Number(params.yieldMs) : undefined;
  const notifyFieldPresent =
    params && typeof params === "object" && !Array.isArray(params) && Object.prototype.hasOwnProperty.call(params, "notifyOnExit");
  const notifyOnExit = notifyFieldPresent ? Boolean(params.notifyOnExit) : null; // null => auto (default)
  const env =
    params?.env && typeof params.env === "object" && !Array.isArray(params.env)
      ? Object.fromEntries(
          Object.entries(params.env).filter(
            ([k, v]) => typeof k === "string" && typeof v === "string",
          ),
        )
      : undefined;

  if (!STORE) {
    return { ok: false, error: { code: "NOT_READY", message: "node store not initialized" } };
  }

  try {
    return await STORE.run({
      argv,
      cwd,
      env,
      timeoutMs,
      yieldMs,
      notifyOnExit,
    });
  } catch (err) {
    return { ok: false, error: { code: "SPAWN_FAILED", message: String(err?.message || err) } };
  }
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
  if (cmd === "process.list") return { ok: true, payload: { jobs: STORE ? STORE.listJobs() : [] } };
  if (cmd === "process.get") {
    const jobId = typeof params?.jobId === "string" ? params.jobId.trim() : "";
    if (!jobId) return { ok: false, error: { code: "BAD_INPUT", message: "process.get requires jobId" } };
    const meta = STORE ? STORE.getJob(jobId) : null;
    if (!meta) return { ok: false, error: { code: "NOT_FOUND", message: `unknown jobId: ${jobId}` } };
    return { ok: true, payload: { job: meta } };
  }
  if (cmd === "process.logs") {
    const jobId = typeof params?.jobId === "string" ? params.jobId.trim() : "";
    if (!jobId) return { ok: false, error: { code: "BAD_INPUT", message: "process.logs requires jobId" } };
    const tailBytes = params?.tailBytes;
    const logs = STORE ? STORE.getLogs(jobId, { tailBytes }) : null;
    if (!logs) return { ok: false, error: { code: "NOT_FOUND", message: `unknown jobId: ${jobId}` } };
    return { ok: true, payload: logs };
  }
  if (cmd === "process.kill") {
    const jobId = typeof params?.jobId === "string" ? params.jobId.trim() : "";
    if (!jobId) return { ok: false, error: { code: "BAD_INPUT", message: "process.kill requires jobId" } };
    return await STORE.kill(jobId);
  }
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
  const commands = [
    "system.run",
    "system.which",
    "process.list",
    "process.get",
    "process.logs",
    "process.kill",
  ];

  const stateDir = resolveStateDir({ nodeId });
  // eslint-disable-next-line no-console
  console.error(`Node state dir: ${stateDir}`);

  let activeWs = null;
  const pendingEvents = [];
  SEND_EVENT = (event, payload) => {
    const frame = { type: "event", event, payload };
    if (activeWs && activeWs.readyState === WebSocket.OPEN) {
      try {
        activeWs.send(JSON.stringify(frame));
        return;
      } catch {
        // fall through
      }
    }
    pendingEvents.push(frame);
  };

  STORE = new JobStore({ stateDir, nodeId, sendEvent: (event, payload) => SEND_EVENT(event, payload) });

  const reconnectDelayMs = 1000;

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const ws = new WebSocket(url);

    await new Promise((resolve) => ws.once("open", resolve));
    activeWs = ws;
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

    while (pendingEvents.length && ws.readyState === WebSocket.OPEN) {
      const frame = pendingEvents.shift();
      try {
        ws.send(JSON.stringify(frame));
      } catch {
        pendingEvents.unshift(frame);
        break;
      }
    }

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
    activeWs = null;
    if (!closed) return;
    await new Promise((r) => setTimeout(r, reconnectDelayMs));
  }
}

run().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err);
  process.exit(1);
});
