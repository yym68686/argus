# argus (Go node-host)

Build for your current platform:

```bash
go build -o argus ./cmd/argus
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

Run:

```bash
./argus --url "ws://127.0.0.1:8080/nodes/ws?token=argus-node-v1.<sessionId>.<sig>" --node-id "mac"
```

Interactive jobs:

- Use `system.run` with `pty: true` to allocate a pseudo-terminal.
- Set `yieldMs: 0` to get a running `jobId` back immediately.
- Continue the session with `process.write`, `process.send_keys`, `process.submit`, or `process.paste`.
