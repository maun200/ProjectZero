"""Local ProjectZero app server (127.0.0.1 only).

Serves the single-page UI, keeps the user/machine tokens out of the browser, forwards a fixed
set of coordinator API calls and runs this computer's worker in-process when sharing is on.

Protection against other websites in the same browser: every request must carry the Host of
this server (DNS rebinding) and the per-start session secret that is embedded in the page
(cross-site requests cannot read the page, so they cannot learn it).
"""
import asyncio
import os
import platform
import secrets
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from worker.client import Worker

from . import __version__
from .config import Config, check_coordinator_url, clamp_settings

OLLAMA = "http://127.0.0.1:11434"
STATIC = Path(__file__).with_name("static")
PROXY_PREFIXES = ("me", "jobs", "channels", "direct", "online", "public/config", "stats", "balance")


class ConnectIn(BaseModel):
    coordinator_url: str = Field(min_length=8, max_length=300)


class RegisterIn(BaseModel):
    name: str = Field(min_length=3, max_length=24)
    invite_code: str | None = Field(default=None, max_length=40)


class LoginIn(BaseModel):
    user_token: str = Field(min_length=10, max_length=200)


class MachineIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=80)


class PullIn(BaseModel):
    model: str = Field(min_length=1, max_length=80)


class WorkerRunner:
    """Runs worker.client.Worker as an asyncio task; restarts on settings changes."""

    def __init__(self, config: Config, transport=None):
        self.config = config
        self.transport = transport
        self.task: asyncio.Task | None = None
        self.worker: Worker | None = None
        self.error: str | None = None

    @property
    def status_path(self) -> Path:
        return self.config.dir / "status.json"

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task, self.worker = None, None

    async def apply(self) -> None:
        await self.stop()
        self.error = None
        settings = self.config.get("settings", {})
        machine = self.config.get("machine")
        url = self.config.get("coordinator_url")
        if not (settings.get("contribute") and machine and url):
            return
        backend = settings.get("backend", "ollama")
        try:
            self.worker = Worker(url, machine["worker_token"], machine["model"], f"{OLLAMA}/api/chat",
                                 backend=backend, context_tokens=settings["context_tokens"],
                                 cpu_threads=settings["cpu_threads"],
                                 max_output_tokens=settings["max_output_tokens"],
                                 gpu_layers=settings["gpu_layers"], status_path=str(self.status_path))
        except ValueError as exc:
            self.error = str(exc)
            return
        self.worker.transport = self.transport
        self.task = asyncio.create_task(self.worker.run())

    def status(self) -> dict:
        info = self.worker.read_status() if self.worker else {}
        return {"running": bool(self.task and not self.task.done()), "error": self.error, **info}


def hardware() -> dict:
    return {"platform": platform.system(), "architecture": platform.machine(),
            "cpu_logical": os.cpu_count() or 1,
            "nvidia_gpu": shutil.which("nvidia-smi") is not None}


def create_app(config: Config | None = None, port: int = 8790, session: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    config = config or Config()
    session = session or secrets.token_urlsafe(24)
    runner = WorkerRunner(config, transport)
    pulls: dict[str, str] = {}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def coordinator_client(timeout: float = 20) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, trust_env=False, transport=transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runner.apply()
        try:
            yield
        finally:
            await runner.stop()

    app = FastAPI(title="ProjectZero", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.runner, app.state.config, app.state.session = runner, config, session

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Unexpected host"}, status_code=421)
        if request.url.path != "/" and not secrets.compare_digest(
                request.headers.get("x-pz-session", "").encode(), session.encode()):
            return JSONResponse({"detail": "Missing session"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index():
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__PZ_SESSION__", session)
        return HTMLResponse(html, headers={"Content-Security-Policy":
                                           "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                           "script-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})

    def require_account() -> tuple[str, str]:
        url, token = config.get("coordinator_url"), config.get("user_token")
        if not url or not token:
            raise HTTPException(409, "Not signed in yet")
        return url, token

    async def coordinator(method: str, path: str, token: str | None = None, **kwargs):
        url = config.get("coordinator_url")
        if not url:
            raise HTTPException(409, "No server connected yet")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with coordinator_client() as client:
                response = await client.request(method, f"{url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Server not reachable: {exc.__class__.__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:300]}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise HTTPException(response.status_code, detail)
        return body

    async def ollama_models() -> list[str] | None:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(f"{OLLAMA}/api/tags")
                response.raise_for_status()
                return sorted(m["name"] for m in response.json().get("models", []))
        except Exception:
            return None

    # ------------------------------------------------------------ local state
    @app.get("/local/state")
    async def state():
        models = await ollama_models()
        machine = config.get("machine")
        return {"version": __version__, "coordinator_url": config.get("coordinator_url"),
                "signed_in": bool(config.get("user_token")), "name": config.get("name"),
                "machine": {k: v for k, v in machine.items() if k != "worker_token"} if machine else None,
                "settings": config.get("settings"), "worker": runner.status(), "hardware": hardware(),
                "ollama": {"available": models is not None, "models": models or []}, "pulls": pulls}

    @app.post("/local/connect")
    async def connect(payload: ConnectIn):
        try:
            url = check_coordinator_url(payload.coordinator_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            async with coordinator_client(10) as client:
                health = (await client.get(f"{url}/health")).json()
                public = (await client.get(f"{url}/api/public/config")).json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "No ProjectZero server answers at this address") from None
        if health.get("status") != "ok":
            raise HTTPException(502, "The server reports an unhealthy state")
        if url != config.get("coordinator_url"):
            await runner.stop()
            config.forget_account()
        config.update(coordinator_url=url)
        return public

    @app.post("/local/register")
    async def register(payload: RegisterIn):
        result = await coordinator("POST", "/api/register", json=payload.model_dump(exclude_none=True))
        config.update(user_token=result["user_token"], name=result["name"])
        return {"name": result["name"], "credits": result["credits"]}

    @app.post("/local/login")
    async def login(payload: LoginIn):
        me = await coordinator("GET", "/api/me", token=payload.user_token.strip())
        config.update(user_token=payload.user_token.strip(), name=me.get("name"))
        return {"name": me.get("name"), "credits": me.get("credits")}

    @app.post("/local/logout")
    async def logout():
        await runner.stop()
        config.forget_account()
        return {"ok": True}

    @app.get("/local/token")
    def reveal_token():
        """For moving the account to another device; the UI shows it only on explicit request."""
        _, token = require_account()
        return {"user_token": token}

    @app.post("/local/machine")
    async def add_machine(payload: MachineIn):
        _, token = require_account()
        old = config.get("machine")
        result = await coordinator("POST", "/api/me/machines", token=token, json=payload.model_dump())
        if old:
            with suppress(HTTPException):
                await coordinator("DELETE", f"/api/me/machines/{old['id']}", token=token)
        config.update(machine={"id": result["id"], "name": result["name"], "model": result["model"],
                               "worker_token": result["worker_token"]})
        await runner.apply()
        return {k: v for k, v in result.items() if k != "worker_token"}

    @app.post("/local/settings")
    async def settings(values: dict):
        merged = clamp_settings({**config.get("settings", {}), **values})
        config.update(settings=merged)
        await runner.apply()
        return {"settings": merged, "worker": runner.status()}

    @app.post("/local/ollama/pull")
    async def pull(payload: PullIn):
        public = await coordinator("GET", "/api/public/config")
        if payload.model not in public.get("allowed_models", []):
            raise HTTPException(422, "This model is not approved on the server")
        if pulls.get(payload.model) == "running":
            return {"status": "running"}

        async def run():
            pulls[payload.model] = "running"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=5), trust_env=False) as client:
                    response = await client.post(f"{OLLAMA}/api/pull", json={"model": payload.model, "stream": False})
                    response.raise_for_status()
                pulls[payload.model] = "done"
            except Exception as exc:
                pulls[payload.model] = f"error: {exc.__class__.__name__}"

        asyncio.create_task(run())
        return {"status": "running"}

    # ------------------------------------------------------------ coordinator proxy
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        if not path.startswith(PROXY_PREFIXES) or ".." in path:
            raise HTTPException(404, "Not available through the local app")
        if path == "me/rotate-token":
            raise HTTPException(404, "Use the account menu")  # would desync the stored token
        token = config.get("user_token")
        if not token and not path.startswith(("public/config", "stats")):
            raise HTTPException(409, "Not signed in yet")
        body = await request.body()
        kwargs = {"params": dict(request.query_params)}
        if body:
            kwargs["content"] = body
            kwargs["headers"] = {"Content-Type": "application/json"}
        url = config.get("coordinator_url")
        if not url:
            raise HTTPException(409, "No server connected yet")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        headers.update(kwargs.pop("headers", {}))
        try:
            async with coordinator_client(30) as client:
                response = await client.request(request.method, f"{url}/api/{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Server not reachable: {exc.__class__.__name__}") from None
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    return app
