"""`projectzero` command line.

    projectzero                  start the app (opens the browser)
    projectzero server [--lan]   host your own coordinator (secrets are generated once)
    projectzero demo             coordinator + app with a mock model, all on this computer
"""
import argparse
import functools
import asyncio
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

print = functools.partial(print, flush=True)  # noqa: A001 - show status lines at once

from . import __version__
from .config import Config, config_dir


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free local port")


def open_later(url: str) -> None:
    def run():
        time.sleep(1.2)
        webbrowser.open(url)
    threading.Thread(target=run, daemon=True).start()


def app_server(config: Config, port: int) -> uvicorn.Server:
    from .app import create_app
    app = create_app(config, port=port)
    return uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))


def server_secrets(directory: Path) -> dict:
    """Admin and demo keys for a self-hosted coordinator, created once with owner-only access."""
    path = directory / "server.env"
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip()
    if not (values.get("CAG_ADMIN_KEY") and values.get("CAG_USER_KEY")):
        values = {"CAG_ADMIN_KEY": secrets.token_urlsafe(32), "CAG_USER_KEY": secrets.token_urlsafe(32)}
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(f"{k}={v}\n" for k, v in values.items()))
    return values


def coordinator_server(directory: Path, host: str, port: int, registration: str, models: list[str],
                       auto_settle: bool, signup_credits: int) -> uvicorn.Server:
    from coordinator.app.main import Settings, create_app
    keys = server_secrets(directory)
    settings = Settings(admin_key=keys["CAG_ADMIN_KEY"], user_key=keys["CAG_USER_KEY"],
                        db_path=str(directory / "grid.sqlite3"), sweep_seconds=30,
                        allowed_models=tuple(models), rate_limit_per_minute=120, registration=registration,
                        auto_settle=auto_settle, signup_credits=signup_credits)
    return uvicorn.Server(uvicorn.Config(create_app(settings), host=host, port=port, log_level="warning"))


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="projectzero", description="ProjectZero - share computing power for AI")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    app_cmd = sub.add_parser("app", help="start the app (default)")
    app_cmd.add_argument("--port", type=int, default=8790)
    app_cmd.add_argument("--no-browser", action="store_true")
    srv = sub.add_parser("server", help="run your own server (coordinator)")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--lan", action="store_true", help="reachable from your whole local network (default: this computer only)")
    srv.add_argument("--registration", choices=["open", "invite", "off"], default="open")
    srv.add_argument("--models", default="qwen2.5-coder:1.5b,demo-model",
                     help="comma-separated list of allowed models")
    srv.add_argument("--auto-settle", action="store_true",
                     help="credit completed jobs immediately (unverified!)")
    srv.add_argument("--signup-credits", type=int, default=20)
    demo = sub.add_parser("demo", help="try everything locally with a demo model")
    demo.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    command = args.command or "app"
    if command == "app":
        port = free_port(getattr(args, "port", 8790))
        config = Config()
        url = f"http://127.0.0.1:{port}/"
        print(f"ProjectZero is running at {url}  (stop: Ctrl+C)")
        if not getattr(args, "no_browser", False):
            open_later(url)
        app_server(config, port).run()
    elif command == "server":
        host = "0.0.0.0" if args.lan else "127.0.0.1"
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        directory = config_dir() / "server"
        server = coordinator_server(directory, host, args.port, args.registration, models,
                                    args.auto_settle, args.signup_credits)
        shown = lan_address() if args.lan else "127.0.0.1"
        print(f"ProjectZero server: http://{shown}:{args.port}  (registration: {args.registration})")
        if args.lan:
            print("Note: on your LAN the server is reachable without encryption. Do not expose it to the"
                  " internet directly; put an HTTPS reverse proxy in front (see docs/SECURITY.md).", file=sys.stderr)
        print(f"Admin key stored in {directory / 'server.env'} (readable by you only).")
        server.run()
    elif command == "demo":
        directory = config_dir() / "demo"
        coord_port, app_port = free_port(8766), free_port(8791)
        coordinator = coordinator_server(directory / "server", "127.0.0.1", coord_port, "open", ["demo-model"],
                                         True, 50)
        config = Config(directory / "client")
        if config.get("coordinator_url") != f"http://127.0.0.1:{coord_port}":
            config.forget_account()
            config.update(coordinator_url=f"http://127.0.0.1:{coord_port}",
                          settings={**config.get("settings"), "backend": "mock", "contribute": False})
        app = app_server(config, app_port)
        url = f"http://127.0.0.1:{app_port}/"
        print(f"Demo running: app {url}, server http://127.0.0.1:{coord_port}  (stop: Ctrl+C)")
        print("Create an account, switch on 'Share computing power' on the right (execution: Demo) and ask the AI.")
        if not args.no_browser:
            open_later(url)

        async def both():
            await asyncio.gather(coordinator.serve(), app.serve())
        try:
            asyncio.run(both())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
