import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from starlette.websockets import WebSocketState


log = logging.getLogger("argus_gateway")


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    token: Optional[str] = None


@dataclass(frozen=True)
class DockerProvisionConfig:
    image: str
    network: str
    home_host_path: Optional[str]
    workspace_host_path: Optional[str]
    home_container_path: str = "/agent-home"
    workspace_container_path: str = "/workspace"
    runtime_cmd: Optional[str] = None
    connect_timeout_s: float = 30.0
    container_prefix: str = "argus-session"


def _provision_mode() -> str:
    return os.getenv("ARGUS_PROVISION_MODE", "static").strip().lower()


def _load_upstreams() -> dict[str, Upstream]:
    raw = os.getenv("ARGUS_UPSTREAMS_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("Invalid ARGUS_UPSTREAMS_JSON; must be valid JSON") from e

        out: dict[str, Upstream] = {}
        for key, cfg in data.items():
            out[str(key)] = Upstream(
                host=str(cfg.get("host", "127.0.0.1")),
                port=int(cfg.get("port", 7777)),
                token=(str(cfg["token"]) if "token" in cfg and cfg["token"] is not None else None),
            )
        if not out:
            raise RuntimeError("ARGUS_UPSTREAMS_JSON is empty")
        return out

    host = os.getenv("ARGUS_TCP_HOST", "127.0.0.1")
    port = int(os.getenv("ARGUS_TCP_PORT", "7777"))
    token = os.getenv("ARGUS_TOKEN")
    return {"default": Upstream(host=host, port=port, token=token)}


UPSTREAMS = _load_upstreams()


def _extract_token(ws: WebSocket) -> Optional[str]:
    auth = ws.headers.get("authorization") or ws.headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
    return ws.query_params.get("token")


def _extract_token_http(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
    return request.query_params.get("token")


def _is_token_valid(expected: Optional[str], provided: Optional[str]) -> bool:
    if expected is None:
        return True
    return provided == expected


app = FastAPI(title="Argus gateway", version="0.1.0")

origins_raw = os.getenv("ARGUS_CORS_ORIGINS") or ""
cors_origins = [o.strip() for o in origins_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    app.state.active_sessions_lock = asyncio.Lock()
    app.state.active_sessions: set[str] = set()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return PlainTextResponse("Argus gateway is running. Optional web UI runs separately (see README.md).")


@app.get("/chat")
async def chat():
    return PlainTextResponse("No built-in UI. Start the optional web UI separately (see README.md).")


@app.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def _http_require_token(request: Request):
    expected = os.getenv("ARGUS_TOKEN") or None
    if expected is None:
        return
    provided = _extract_token_http(request)
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _docker_cfg() -> DockerProvisionConfig:
    image = os.getenv("ARGUS_RUNTIME_IMAGE", "argus-runtime")
    network = os.getenv("ARGUS_DOCKER_NETWORK", "argus-net")

    home_host_path = os.getenv("ARGUS_HOME_HOST_PATH") or None
    workspace_host_path = os.getenv("ARGUS_WORKSPACE_HOST_PATH") or None
    runtime_cmd = os.getenv("ARGUS_RUNTIME_CMD") or None

    if home_host_path and not os.path.isabs(home_host_path):
        raise RuntimeError("ARGUS_HOME_HOST_PATH must be an absolute host path")
    if workspace_host_path and not os.path.isabs(workspace_host_path):
        raise RuntimeError("ARGUS_WORKSPACE_HOST_PATH must be an absolute host path")

    connect_timeout_s = float(os.getenv("ARGUS_CONNECT_TIMEOUT_S", "30"))
    container_prefix = os.getenv("ARGUS_CONTAINER_PREFIX", "argus-session")

    return DockerProvisionConfig(
        image=image,
        network=network,
        home_host_path=home_host_path,
        workspace_host_path=workspace_host_path,
        runtime_cmd=runtime_cmd,
        connect_timeout_s=connect_timeout_s,
        container_prefix=container_prefix,
    )


def _docker_create_container_sync(cfg: DockerProvisionConfig, session_id: str):
    try:
        import docker  # type: ignore
        from docker.errors import ImageNotFound, NotFound  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()

    try:
        client.networks.get(cfg.network)
    except NotFound as e:
        raise RuntimeError(
            f"Docker network '{cfg.network}' not found. "
            "Create it (or run via docker-compose that defines it) and attach the gateway to it."
        ) from e

    name = f"{cfg.container_prefix}-{session_id}"

    env = {}
    if cfg.runtime_cmd:
        env["APP_SERVER_CMD"] = cfg.runtime_cmd
    env["APP_HOME"] = cfg.home_container_path
    volumes = {}
    if cfg.home_host_path:
        volumes[cfg.home_host_path] = {"bind": cfg.home_container_path, "mode": "rw"}
    if cfg.workspace_host_path:
        volumes[cfg.workspace_host_path] = {"bind": cfg.workspace_container_path, "mode": "rw"}

    labels = {
        "io.argus.gateway": "apps/api",
        "io.argus.session_id": session_id,
    }

    run_kwargs = {}
    if cfg.workspace_host_path:
        run_kwargs["working_dir"] = cfg.workspace_container_path

    try:
        container = client.containers.run(
            cfg.image,
            name=name,
            detach=True,
            network=cfg.network,
            environment=env,
            volumes=volumes,
            labels=labels,
            **run_kwargs,
        )
    except ImageNotFound as e:
        raise RuntimeError(
            f"Docker image '{cfg.image}' not found. Build it first (e.g. 'docker build -t {cfg.image} ...')."
        ) from e

    return container


def _docker_container_ip_sync(container, network: str) -> Optional[str]:
    container.reload()
    nets = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
    net = nets.get(network)
    if not net:
        return None
    ip = net.get("IPAddress")
    return ip or None


def _docker_remove_container_sync(container):
    try:
        container.remove(force=True)
    except Exception:
        pass


def _docker_list_argus_containers_sync():
    try:
        import docker  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()
    containers = client.containers.list(
        all=True,
        filters={"label": ["io.argus.gateway=apps/api"]},
    )

    out = []
    for c in containers:
        labels = getattr(c, "labels", None) or {}
        out.append(
            {
                "sessionId": labels.get("io.argus.session_id"),
                "containerId": c.id,
                "name": c.name,
                "status": c.status,
            }
        )

    out.sort(key=lambda x: (x.get("status") != "running", x.get("name") or ""))
    return out


def _docker_get_container_by_session_sync(session_id: str):
    try:
        import docker  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                "io.argus.gateway=apps/api",
                f"io.argus.session_id={session_id}",
            ]
        },
    )
    return containers[0] if containers else None


def _docker_ensure_running_sync(container):
    container.reload()
    if container.status != "running":
        container.start()
        container.reload()
    return container


async def _docker_wait_for_ip(container, network: str, timeout_s: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_ip: Optional[str] = None
    while True:
        last_ip = await asyncio.to_thread(_docker_container_ip_sync, container, network)
        if last_ip:
            return last_ip
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Timed out waiting for container IP")
        await asyncio.sleep(0.1)


async def _wait_for_tcp(host: str, port: int, timeout_s: float):
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_err: Optional[Exception] = None
    while True:
        try:
            return await asyncio.open_connection(host, port)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if asyncio.get_running_loop().time() >= deadline:
                raise last_err
            await asyncio.sleep(0.2)


@app.get("/sessions")
async def list_sessions(request: Request):
    _http_require_token(request)
    if _provision_mode() != "docker":
        raise HTTPException(status_code=400, detail="Not in docker provision mode")
    try:
        sessions = await asyncio.to_thread(_docker_list_argus_containers_sync)
    except Exception as e:
        log.exception("Failed to list docker sessions")
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"sessions": sessions}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    _http_require_token(request)
    if _provision_mode() != "docker":
        raise HTTPException(status_code=400, detail="Not in docker provision mode")
    try:
        container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        if container is None:
            raise HTTPException(status_code=404, detail="Session not found")
        await asyncio.to_thread(_docker_remove_container_sync, container)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to delete docker session %s", session_id)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "sessionId": session_id}


@app.websocket("/ws")
async def ws_proxy(ws: WebSocket):
    provided = _extract_token(ws)

    if _provision_mode() == "docker":
        expected = os.getenv("ARGUS_TOKEN")
        if expected and provided != expected:
            await ws.accept()
            await ws.close(code=1008, reason="Unauthorized")
            return

        await ws.accept()

        try:
            cfg = _docker_cfg()
        except Exception:
            log.exception("Invalid docker provisioning configuration")
            await ws.close(code=1011, reason="Server misconfigured")
            return
        if not cfg.runtime_cmd:
            await ws.close(code=1011, reason="Server misconfigured: ARGUS_RUNTIME_CMD is not set")
            return
        requested_session = (ws.query_params.get("session") or "").strip() or None
        session_id = requested_session or uuid.uuid4().hex[:12]

        container = None
        reader = None
        writer = None

        async with app.state.active_sessions_lock:
            if session_id in app.state.active_sessions:
                await ws.close(code=1008, reason="Session already connected")
                return
            app.state.active_sessions.add(session_id)

        try:
            try:
                if requested_session:
                    container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
                    if container is None:
                        await ws.close(code=1008, reason="Unknown session")
                        return
                    container = await asyncio.to_thread(_docker_ensure_running_sync, container)
                else:
                    container = await asyncio.to_thread(_docker_create_container_sync, cfg, session_id)

                host = await _docker_wait_for_ip(container, cfg.network, cfg.connect_timeout_s)
                reader, writer = await _wait_for_tcp(host, 7777, cfg.connect_timeout_s)
            except Exception:
                await ws.close(code=1011, reason="Failed to provision upstream container")
                return

            log.info("Provisioned session %s -> %s:%s", session_id, host, 7777)
            try:
                await ws.send_text(
                    json.dumps(
                        {
                            "method": "argus/session",
                            "params": {
                                "id": session_id,
                                "mode": "docker",
                                "attached": bool(requested_session),
                            },
                        }
                    )
                )
            except Exception:
                pass

            try:
                async def ws_to_tcp():
                    try:
                        while True:
                            text = await ws.receive_text()
                            if not text.endswith("\n"):
                                text += "\n"
                            writer.write(text.encode("utf-8"))
                            await writer.drain()
                    except WebSocketDisconnect:
                        pass
                    finally:
                        try:
                            writer.close()
                        except Exception:
                            pass

                async def tcp_to_ws():
                    try:
                        while True:
                            line = await reader.readline()
                            if not line:
                                break
                            await ws.send_text(line.decode("utf-8").rstrip("\n"))
                    finally:
                        if ws.client_state == WebSocketState.CONNECTED:
                            try:
                                await ws.close()
                            except Exception:
                                pass

                a = asyncio.create_task(ws_to_tcp())
                b = asyncio.create_task(tcp_to_ws())

                done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    try:
                        task.result()
                    except Exception:
                        pass
            finally:
                try:
                    if writer is not None:
                        writer.close()
                except Exception:
                    pass
                log.info("Session %s detached (container retained)", session_id)
        finally:
            async with app.state.active_sessions_lock:
                app.state.active_sessions.discard(session_id)

        return

    upstream_id = ws.query_params.get("id")
    if upstream_id:
        upstream = UPSTREAMS.get(upstream_id)
        if not upstream:
            await ws.accept()
            await ws.close(code=1008, reason="Unknown upstream id")
            return
    else:
        if len(UPSTREAMS) == 1:
            upstream = next(iter(UPSTREAMS.values()))
        else:
            matches = [u for u in UPSTREAMS.values() if _is_token_valid(u.token, provided)]
            if len(matches) != 1:
                await ws.accept()
                await ws.close(code=1008, reason="Unauthorized")
                return
            upstream = matches[0]

    if not _is_token_valid(upstream.token, provided):
        await ws.accept()
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()

    try:
        reader, writer = await asyncio.open_connection(upstream.host, upstream.port)
    except Exception:
        await ws.close(code=1011, reason="Failed to connect to upstream")
        return

    async def ws_to_tcp():
        try:
            while True:
                text = await ws.receive_text()
                if not text.endswith("\n"):
                    text += "\n"
                writer.write(text.encode("utf-8"))
                await writer.drain()
        except WebSocketDisconnect:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                await ws.send_text(line.decode("utf-8").rstrip("\n"))
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.close()
                except Exception:
                    pass

    a = asyncio.create_task(ws_to_tcp())
    b = asyncio.create_task(tcp_to_ws())

    done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except Exception:
            pass
