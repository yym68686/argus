# argus (Go host-agent)

This module now ships:

- `cmd/argus`: unified host-agent CLI for one-command enrollment (`/host-agent/ws`)
- `cmd/runtime-host`: legacy native Codex runtime host (`/runtime-host/ws`)

Build for your current platform:

```bash
go build -o argus ./cmd/argus
go build -o runtime-host ./cmd/runtime-host
```

Cross-compile:

```bash
# macOS arm64
GOOS=darwin GOARCH=arm64 go build -o dist/argus-darwin-arm64 ./cmd/argus

# Linux amd64
GOOS=linux GOARCH=amd64 go build -o dist/argus-linux-amd64 ./cmd/argus

# Windows amd64
GOOS=windows GOARCH=amd64 go build -o dist/argus-windows-amd64.exe ./cmd/argus
```

One-command host takeover:

```bash
./argus connect \
  --gateway "https://argus.example.com" \
  --enroll-token "argus-host-enroll-v1.<tokenId>.<secret>" \
  --default
```

This claims the one-time enrollment token, saves a local device credential, and starts a unified host-agent that provides:

- native Codex runtime relay
- `system.run` / `process.*`
- local MCP/browser inheritance via the local Codex profile

Run from saved config later:

```bash
./argus daemon
```

Refresh the local CLI from the enrolled gateway:

```bash
./argus upgrade
```

Inspect or revoke the local enrollment:

```bash
./argus status
./argus disconnect
```

Legacy node control plane:

```bash
./argus --url "ws://127.0.0.1:8080/nodes/ws?token=argus-node-v1.<sessionId>.<sig>" --node-id "mac"
```

Interactive jobs on the legacy node plane:

- Use `system.run` with `pty: true` to allocate a pseudo-terminal.
- Set `yieldMs: 0` to get a running `jobId` back immediately.
- Continue the session with `process.write`, `process.send_keys`, `process.submit`, or `process.paste`.

Legacy standalone native runtime host:

```bash
./runtime-host \
  --url "ws://127.0.0.1:8080/runtime-host/ws?token=argus-runtime-host-v1.<hostId>.<sig>" \
  --host-id "my-mac"
```

Useful paths for the unified `argus` host-agent:

- config: `~/.argus/host-agent.json`
- workspace base: `~/.argus/native-workspaces`
- session Codex overlays: `~/.argus/native-codex`

Useful env vars for the legacy `runtime-host`:

- `ARGUS_RUNTIME_HOST_WS_URL`: gateway `/runtime-host/ws` URL with token
- `ARGUS_RUNTIME_HOST_ID`: stable host id
- `ARGUS_RUNTIME_HOST_DISPLAY_NAME`: friendly host label
- `ARGUS_RUNTIME_HOST_WORKSPACE_BASE_PATH`: default workspace base for native sessions
- `ARGUS_RUNTIME_HOST_CODEX_HOME_BASE_PATH`: per-session `CODEX_HOME` overlay root
- `ARGUS_RUNTIME_HOST_APP_SERVER_CMD`: app-server launch command, default `codex app-server --listen stdio://`
