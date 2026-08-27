"""A local web interface for browsing chats and managing downloads.

Binds to loopback only. It drives exactly the same code paths as the CLI —
`collect` to find videos, `run` to fetch them — so behaviour can't drift
between the two front ends.
"""

from __future__ import annotations

import asyncio
import ipaddress
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Channel, Chat, User

from .client import ChatNotFound, make_client, secure_session
from .config import ConfigError, load_config, save_config, session_path
from .downloader import CallbackReporter, Options, collect, run
from .media import GIF, VIDEO, VIDEO_NOTE, VideoItem
from .state import State

STATIC = Path(__file__).parent / "static"

# Steps the browser walks through before the app is usable.
STEP_CREDENTIALS = "credentials"   # no api_id/api_hash on this machine yet
STEP_PHONE = "phone"               # have credentials, need a number
STEP_CODE = "code"                 # code sent, waiting for it
STEP_PASSWORD = "password"         # 2FA is on
STEP_READY = "ready"


@dataclass
class FileProgress:
    msg_id: int
    filename: str
    size: int
    done: int = 0
    status: str = "queued"  # queued | downloading | done | skipped | failed

    def as_json(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "filename": self.filename,
            "size": self.size,
            "done": self.done,
            "status": self.status,
        }


@dataclass
class Job:
    id: str
    chat_id: int
    chat_title: str
    out_dir: str
    files: dict[int, FileProgress]
    status: str = "running"  # running | done | cancelled | error
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def as_json(self) -> dict:
        files = list(self.files.values())
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "out_dir": self.out_dir,
            "status": self.status,
            "error": self.error,
            "total": len(files),
            "finished": sum(1 for f in files if f.status in ("done", "skipped")),
            "failed": sum(1 for f in files if f.status == "failed"),
            "bytes_total": sum(f.size for f in files),
            "bytes_done": sum(f.done if f.status != "done" else f.size for f in files),
            "files": [f.as_json() for f in files],
        }


class Server:
    def __init__(self, client, root: Path, session_name: str = "default",
                 client_factory=None):
        self.client = client
        self.root = root
        self.session_name = session_name
        # Injectable so the login flow can be tested without a real connection.
        self.client_factory = client_factory or (
            lambda: make_client(load_config(), session_name)
        )
        self.jobs: dict[str, Job] = {}
        self.entities: dict[int, object] = {}
        self.scans: dict[int, list[VideoItem]] = {}
        self.phone: str | None = None
        self.awaiting_password = False

    # ---- auth ----------------------------------------------------------

    async def ensure_client(self):
        """Build and connect the client once credentials exist on disk."""
        if self.client is None:
            self.client = self.client_factory()
        if not self.client.is_connected():
            await self.client.connect()
        secure_session(self.session_name)
        return self.client

    async def current_step(self) -> str:
        try:
            await self.ensure_client()
        except ConfigError:
            return STEP_CREDENTIALS
        if await self.client.is_user_authorized():
            return STEP_READY
        if self.awaiting_password:
            return STEP_PASSWORD
        return STEP_CODE if self.phone else STEP_PHONE

    async def authorized(self) -> bool:
        return await self.current_step() == STEP_READY

    async def auth_status(self, _request):
        step = await self.current_step()
        payload = {"step": step}
        if step == STEP_READY:
            user = await self.client.get_me()
            payload["me"] = {
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                "username": user.username,
                "id": user.id,
            }
        payload["root"] = str(self.root)
        return web.json_response(payload)

    async def auth_credentials(self, request):
        body = await request.json()
        try:
            api_id = int(str(body.get("api_id", "")).strip())
        except ValueError:
            raise web.HTTPBadRequest(text="api_id must be a number") from None
        api_hash = str(body.get("api_hash", "")).strip()
        if not api_hash:
            raise web.HTTPBadRequest(text="api_hash is required")
        save_config(api_id, api_hash)
        self.client = None  # rebuild against the new credentials
        return web.json_response({"step": await self.current_step()})

    async def auth_phone(self, request):
        body = await request.json()
        phone = str(body.get("phone", "")).strip()
        if not phone:
            raise web.HTTPBadRequest(text="phone number is required")
        client = await self.ensure_client()
        try:
            await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            raise web.HTTPBadRequest(text="that phone number isn't valid") from None
        except PhoneNumberBannedError:
            raise web.HTTPBadRequest(text="that number is banned from Telegram") from None
        except ApiIdInvalidError:
            raise web.HTTPBadRequest(
                text="api_id/api_hash rejected — check them at my.telegram.org"
            ) from None
        except FloodWaitError as exc:
            raise web.HTTPTooManyRequests(
                text=f"too many attempts, wait {exc.seconds}s"
            ) from None
        self.phone = phone
        self.awaiting_password = False
        return web.json_response({"step": STEP_CODE})

    async def auth_code(self, request):
        body = await request.json()
        code = str(body.get("code", "")).strip()
        if not self.phone:
            raise web.HTTPBadRequest(text="ask for a code first")
        if not code:
            raise web.HTTPBadRequest(text="code is required")
        client = await self.ensure_client()
        try:
            await client.sign_in(phone=self.phone, code=code)
        except SessionPasswordNeededError:
            self.awaiting_password = True
            return web.json_response({"step": STEP_PASSWORD})
        except PhoneCodeInvalidError:
            raise web.HTTPBadRequest(text="that code isn't right") from None
        except PhoneCodeExpiredError:
            self.phone = None
            raise web.HTTPBadRequest(text="that code expired — request a new one") from None
        except FloodWaitError as exc:
            raise web.HTTPTooManyRequests(
                text=f"too many attempts, wait {exc.seconds}s"
            ) from None
        return web.json_response({"step": await self.finish_login()})

    async def auth_password(self, request):
        body = await request.json()
        password = body.get("password") or ""
        if not password:
            raise web.HTTPBadRequest(text="password is required")
        client = await self.ensure_client()
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise web.HTTPBadRequest(text="that password isn't right") from None
        except FloodWaitError as exc:
            raise web.HTTPTooManyRequests(
                text=f"too many attempts, wait {exc.seconds}s"
            ) from None
        return web.json_response({"step": await self.finish_login()})

    async def finish_login(self) -> str:
        secure_session(self.session_name)
        self.phone = None
        self.awaiting_password = False
        self.entities.clear()
        self.scans.clear()
        return await self.current_step()

    async def auth_logout(self, _request):
        if self.client is not None:
            if not self.client.is_connected():
                await self.client.connect()
            if await self.client.is_user_authorized():
                await self.client.log_out()
            await self.client.disconnect()
        session_path(self.session_name).unlink(missing_ok=True)
        self.client = None
        self.entities.clear()
        self.scans.clear()
        self.jobs.clear()
        self.phone = None
        self.awaiting_password = False
        return web.json_response({"step": await self.current_step()})

    # ---- helpers -------------------------------------------------------

    def chat_dir(self, title: str, chat_id: int) -> Path:
        from .cli import _safe_dirname

        return self.root / f"{_safe_dirname(title)}"

    def inside_root(self, path: Path) -> bool:
        """Guard every filesystem mutation — the browser is not a trusted caller."""
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except (ValueError, OSError):
            return False

    async def entity(self, chat_id: int):
        if chat_id not in self.entities:
            async for dialog in self.client.iter_dialogs():
                self.entities[dialog.id] = dialog.entity
        if chat_id not in self.entities:
            raise ChatNotFound(f"chat {chat_id} not found")
        return self.entities[chat_id]

    # ---- routes --------------------------------------------------------

    async def index(self, _request):
        # Without Cache-Control the browser caches the page heuristically and can
        # serve a stale build for hours without revalidating. The ETag still
        # makes the revalidation a cheap 304.
        return web.FileResponse(
            STATIC / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def me(self, _request):
        user = await self.client.get_me()
        return web.json_response(
            {
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                "username": user.username,
                "id": user.id,
                "root": str(self.root),
            }
        )

    async def chats(self, request):
        needle = (request.query.get("q") or "").casefold()
        rows = []
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            self.entities[dialog.id] = entity
            if isinstance(entity, User):
                continue
            name = dialog.name or ""
            if needle and needle not in name.casefold():
                continue
            if isinstance(entity, Channel):
                kind = "channel" if entity.broadcast else "group"
                private = not entity.username
            elif isinstance(entity, Chat):
                kind, private = "group", True
            else:
                kind, private = "chat", True
            rows.append(
                {
                    "id": dialog.id,
                    "name": name,
                    "kind": kind,
                    "private": private,
                    "protected": bool(getattr(entity, "noforwards", False)),
                }
            )
        return web.json_response(rows)

    async def scan(self, request):
        body = await request.json()
        chat_id = int(body["chat_id"])
        try:
            chat = await self.entity(chat_id)
        except ChatNotFound as exc:
            raise web.HTTPNotFound(text=str(exc)) from None

        kinds = {VIDEO}
        if body.get("gifs"):
            kinds.add(GIF)
        if body.get("notes"):
            kinds.add(VIDEO_NOTE)

        title = getattr(chat, "title", None) or str(chat_id)
        out_dir = self.chat_dir(title, chat_id)
        opts = Options(
            out_dir=out_dir,
            kinds=kinds,
            limit=int(body.get("limit") or 200),
            search=body.get("search") or None,
            min_size=int(body.get("min_size") or 0),
        )
        items = await collect(self.client, chat, opts)
        self.scans[chat_id] = items

        state = State(out_dir / ".telefetcher" / "state.db")
        payload = [
            {
                "msg_id": i.msg_id,
                "kind": i.kind,
                "filename": i.filename,
                "size": i.size,
                "duration": i.duration,
                "have": state.done(chat_id, i.msg_id) is not None,
            }
            for i in items
        ]
        state.close()
        return web.json_response(
            {"chat_id": chat_id, "title": title, "out_dir": str(out_dir), "items": payload}
        )

    async def download(self, request):
        body = await request.json()
        chat_id = int(body["chat_id"])
        wanted = {int(m) for m in body.get("msg_ids") or []}
        items = [i for i in self.scans.get(chat_id, []) if i.msg_id in wanted]
        if not items:
            raise web.HTTPBadRequest(text="nothing to download — scan the chat first")

        chat = await self.entity(chat_id)
        title = getattr(chat, "title", None) or str(chat_id)
        out_dir = self.chat_dir(title, chat_id)
        opts = Options(
            out_dir=out_dir,
            workers=max(1, min(8, int(body.get("workers") or 2))),
            overwrite=bool(body.get("overwrite")),
        )

        job = Job(
            id=uuid.uuid4().hex[:12],
            chat_id=chat_id,
            chat_title=title,
            out_dir=str(out_dir),
            files={i.msg_id: FileProgress(i.msg_id, i.filename, i.size) for i in items},
        )
        self.jobs[job.id] = job

        def make_reporter(item, _position):
            progress = job.files[item.msg_id]
            progress.status = "downloading"
            progress.done = 0

            def on_progress(it, done, _total):
                job.files[it.msg_id].done = done

            return CallbackReporter(item, on_progress)

        def on_done(item):
            progress = job.files[item.msg_id]
            if progress.status == "downloading":
                progress.status = "done"
                progress.done = progress.size

        async def worker():
            state = State(out_dir / ".telefetcher" / "state.db")
            try:
                for item in items:  # mark what's already on disk before starting
                    if not opts.overwrite and state.done(chat_id, item.msg_id):
                        job.files[item.msg_id].status = "skipped"
                summary = await run(
                    self.client, chat, items, opts, state,
                    make_reporter=make_reporter, quiet=True, on_done=on_done,
                )
                for progress in job.files.values():
                    if progress.status == "downloading":
                        progress.status = "failed"
                job.status = "done"
                if summary.errors:
                    job.error = "; ".join(summary.errors[:3])
            except asyncio.CancelledError:
                job.status = "cancelled"
                for progress in job.files.values():
                    if progress.status in ("queued", "downloading"):
                        progress.status = "queued"
                raise
            except Exception as exc:
                job.status, job.error = "error", repr(exc)
            finally:
                state.close()

        job.task = asyncio.create_task(worker())
        return web.json_response({"job_id": job.id})

    async def list_jobs(self, _request):
        # note: named list_jobs, not jobs — self.jobs is the registry dict
        return web.json_response([j.as_json() for j in self.jobs.values()])

    async def cancel(self, request):
        job = self.jobs.get(request.match_info["job_id"])
        if job is None:
            raise web.HTTPNotFound(text="no such job")
        if job.task and not job.task.done():
            job.task.cancel()
        return web.json_response({"ok": True})

    async def clear_jobs(self, _request):
        for job_id, job in list(self.jobs.items()):
            if job.status != "running":
                del self.jobs[job_id]
        return web.json_response({"ok": True})

    async def files(self, request):
        chat_id = request.query.get("chat_id")
        folders = []
        if chat_id:
            chat = await self.entity(int(chat_id))
            title = getattr(chat, "title", None) or str(chat_id)
            folders = [self.chat_dir(title, int(chat_id))]
        elif self.root.exists():
            folders = [p for p in sorted(self.root.iterdir()) if p.is_dir()]

        rows = []
        for folder in folders:
            for path in sorted(folder.glob("*")):
                if path.is_dir() or path.suffix == ".part":
                    continue
                stat = path.stat()
                rows.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "folder": folder.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return web.json_response(rows)

    async def delete_file(self, request):
        body = await request.json()
        path = Path(body["path"])
        if not self.inside_root(path):
            raise web.HTTPForbidden(text="path is outside the downloads folder")
        if not path.is_file():
            raise web.HTTPNotFound(text="no such file")
        path.unlink()
        return web.json_response({"ok": True})

    async def reveal(self, request):
        body = await request.json()
        path = Path(body["path"])
        if not self.inside_root(path):
            raise web.HTTPForbidden(text="path is outside the downloads folder")
        opener = {"darwin": ["open", "-R"], "win32": ["explorer", "/select,"]}.get(
            sys.platform, ["xdg-open"]
        )
        target = str(path) if sys.platform == "darwin" else str(path.parent)
        try:
            subprocess.Popen([*opener, target])
        except OSError as exc:
            raise web.HTTPInternalServerError(text=str(exc)) from None
        return web.json_response({"ok": True})


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
OPEN_PATHS = ("/api/auth/",)  # reachable before sign-in


def is_loopback(addr: str) -> bool:
    """True only for addresses that can't be reached from another machine.

    The Host/Origin guard below stops a web page from calling this API, but a
    Host header is written by whoever sends the request, so it is not an access
    check: bound to 0.0.0.0, anyone on the network can send `Host: localhost`
    and drive the whole signed-in session. Refusing to bind off-loopback is the
    part that actually holds.
    """
    if addr == "localhost":
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def hostname_of(value: str, with_scheme: bool = False) -> str:
    """Hostname from a Host header or an Origin, port and scheme stripped."""
    try:
        parts = urlsplit(value if with_scheme else "//" + value)
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def make_guard(host: str, _port: int):
    """Keep other origins out of an API that holds a live Telegram session.

    A browser will happily send a cross-site request to localhost. Checking the
    Host *name* blocks DNS rebinding (an attacker's domain resolving to
    127.0.0.1 still sends its own name), and checking Origin blocks a page on
    another site from POSTing here. Ports are deliberately ignored — they say
    nothing about who is calling.
    """
    allowed = ALLOWED_HOSTS | {host.lower()}

    @web.middleware
    async def guard(request, handler):
        if hostname_of(request.headers.get("Host") or "") not in allowed:
            raise web.HTTPForbidden(text="bad Host header")
        origin = request.headers.get("Origin")
        if origin:
            if urlsplit(origin).scheme not in ("http", "https") or (
                hostname_of(origin, with_scheme=True) not in allowed
            ):
                raise web.HTTPForbidden(text="cross-origin requests are not allowed")
        return await handler(request)

    return guard


@web.middleware
async def require_login(request, handler):
    """Everything but the auth handshake and the page itself needs a session."""
    path = request.path
    if path.startswith("/api/") and not path.startswith(OPEN_PATHS):
        server = request.app["server"]
        if not await server.authorized():
            raise web.HTTPUnauthorized(text="not signed in")
    return await handler(request)


def build_app(client, root: Path, session_name: str = "default",
              host: str = "127.0.0.1", port: int = 8420,
              client_factory=None) -> web.Application:
    server = Server(client, root, session_name, client_factory)
    app = web.Application(middlewares=[make_guard(host, port), require_login])
    app["server"] = server
    app.add_routes(
        [
            web.get("/", server.index),
            web.get("/api/auth/status", server.auth_status),
            web.post("/api/auth/credentials", server.auth_credentials),
            web.post("/api/auth/phone", server.auth_phone),
            web.post("/api/auth/code", server.auth_code),
            web.post("/api/auth/password", server.auth_password),
            web.post("/api/auth/logout", server.auth_logout),
            web.get("/api/me", server.me),
            web.get("/api/chats", server.chats),
            web.post("/api/scan", server.scan),
            web.post("/api/download", server.download),
            web.get("/api/jobs", server.list_jobs),
            web.post("/api/jobs/{job_id}/cancel", server.cancel),
            web.post("/api/jobs/clear", server.clear_jobs),
            web.get("/api/files", server.files),
            web.post("/api/files/delete", server.delete_file),
            web.post("/api/files/reveal", server.reveal),
        ]
    )
    app.router.add_static("/static/", STATIC)
    return app


async def serve(client, root: Path, host: str, port: int, open_browser: bool,
                session_name: str = "default") -> None:
    root.mkdir(parents=True, exist_ok=True)
    app = build_app(client, root, session_name, host, port)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    url = f"http://{host}:{port}"
    # flush so the banner shows even when stdout is piped to a file
    print(f"telefetcher ui on {url}", flush=True)
    print(f"downloads -> {root}", flush=True)
    print("ctrl-c to stop", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
