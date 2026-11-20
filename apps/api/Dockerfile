FROM golang:1.26-alpine AS node-host-builder

WORKDIR /src/apps/node-host
COPY apps/node-host/ ./
COPY VERSION /src/VERSION
RUN set -eu; \
    version="$(cat /src/VERSION)"; \
    test -n "$version"; \
    ldflags="-s -w -X github.com/yym68686/argus/apps/node-host/internal/version.buildVersion=$version"; \
    mkdir -p /out/host-agent-dist; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-linux-amd64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-linux-arm64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-darwin-amd64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-darwin-arm64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-windows-amd64.exe ./cmd/argus; \
    CGO_ENABLED=0 GOOS=windows GOARCH=arm64 go build -trimpath -ldflags "$ldflags" -o /out/host-agent-dist/argus-windows-arm64.exe ./cmd/argus

FROM python:3.12-slim-trixie

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt /app/apps/api/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/apps/api/requirements.txt

COPY VERSION /app/VERSION
COPY apps/api/app.py /app/apps/api/app.py
COPY docs/templates /app/docs/templates
COPY --from=node-host-builder /out/host-agent-dist/ /app/host-agent-dist/

EXPOSE 8080
WORKDIR /app/apps/api

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
