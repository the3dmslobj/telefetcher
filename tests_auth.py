"""Drives the browser login flow end to end against a stub client."""

import os
import tempfile
from pathlib import Path

# config paths are read at import time, so redirect them before importing the app
CONF_HOME = tempfile.mkdtemp(prefix="tf-auth-")
os.environ["TELEFETCHER_HOME"] = CONF_HOME
os.environ.pop("TG_API_ID", None)
os.environ.pop("TG_API_HASH", None)

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from telethon.errors import (  # noqa: E402
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from telefetcher.config import CONFIG_PATH, load_config  # noqa: E402
from telefetcher.web import build_app  # noqa: E402

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


class StubClient:
    """Mimics the slice of Telethon the login flow touches."""

    def __init__(self, two_factor=False):
        self.two_factor = two_factor
        self.authorized = False
        self.connected = False
        self.codes_sent = []
        self.logged_out = False

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        if not phone.startswith("+"):
            raise PhoneNumberInvalidError(request=None)
        self.codes_sent.append(phone)

    async def sign_in(self, phone=None, code=None, password=None):
        if password is not None:
            if password != "hunter2":
                raise PasswordHashInvalidError(request=None)
            self.authorized = True
            return
        if code != "12345":
            raise PhoneCodeInvalidError(request=None)
        if self.two_factor:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True

    async def get_me(self):
        return SimpleNamespace(first_name="Dana", last_name="", username="dana", id=42)

    def iter_dialogs(self):
        async def gen():
            return
            yield
        return gen()

    async def log_out(self):
        self.logged_out = True
        self.authorized = False

    async def disconnect(self):
        self.connected = False


def make_http(stub, root):
    def factory():
        load_config()  # raises ConfigError until credentials are saved
        return stub

    app = build_app(None, root, "authtest", "127.0.0.1", 8420, client_factory=factory)
    return TestClient(TestServer(app))


async def status(http):
    return await (await http.get("/api/auth/status")).json()


async def main():
    root = Path(CONF_HOME) / "dl"

    print("before any credentials")
    stub = StubClient()
    async with make_http(stub, root) as http:
        check("starts at the credentials step", (await status(http))["step"], "credentials")
        check("data endpoints refuse", (await http.get("/api/chats")).status, 401)
        check("me refuses", (await http.get("/api/me")).status, 401)
        check("page itself still serves", (await http.get("/")).status, 200)

        print("\nsaving credentials")
        bad = await http.post("/api/auth/credentials", json={"api_id": "abc", "api_hash": "x"})
        check("non-numeric api_id rejected", bad.status, 400)
        check("reason given", await bad.text(), "api_id must be a number")
        blank = await http.post("/api/auth/credentials", json={"api_id": "1", "api_hash": " "})
        check("blank api_hash rejected", blank.status, 400)
        check("still at credentials", (await status(http))["step"], "credentials")

        good = await http.post(
            "/api/auth/credentials", json={"api_id": "123456", "api_hash": "a" * 32})
        check("credentials accepted", good.status, 200)
        check("advances to phone", (await good.json())["step"], "phone")
        check("config written", CONFIG_PATH.exists(), True)
        check("config is private", oct(CONFIG_PATH.stat().st_mode & 0o777), "0o600")

        print("\nphone and code")
        bad = await http.post("/api/auth/phone", json={"phone": "5551234"})
        check("bad number rejected", bad.status, 400)
        check("message is human", await bad.text(), "that phone number isn't valid")
        check("empty number rejected",
              (await http.post("/api/auth/phone", json={"phone": ""})).status, 400)

        early = await http.post("/api/auth/code", json={"code": "12345"})
        check("code before phone rejected", early.status, 400)

        sent = await http.post("/api/auth/phone", json={"phone": "+15551234567"})
        check("code requested", (await sent.json())["step"], "code")
        check("stub saw the number", stub.codes_sent, ["+15551234567"])
        check("status agrees", (await status(http))["step"], "code")

        wrong = await http.post("/api/auth/code", json={"code": "00000"})
        check("wrong code rejected", wrong.status, 400)
        check("message is human", await wrong.text(), "that code isn't right")
        check("still awaiting the code", (await status(http))["step"], "code")

        done = await http.post("/api/auth/code", json={"code": "12345"})
        check("correct code signs in", (await done.json())["step"], "ready")

        print("\nsigned in")
        st = await status(http)
        check("status ready", st["step"], "ready")
        check("account returned", st["me"]["name"], "Dana")
        check("api_hash never sent to the browser", "api_hash" in str(st), False)
        check("data endpoints open up", (await http.get("/api/chats")).status, 200)
        check("me works", (await http.get("/api/me")).status, 200)

        print("\nsigning out")
        out = await http.post("/api/auth/logout")
        check("logout ok", out.status, 200)
        check("stub was logged out", stub.logged_out, True)
        check("credentials kept, back to phone", (await out.json())["step"], "phone")
        check("data endpoints locked again", (await http.get("/api/chats")).status, 401)

    print("\ntwo-step verification")
    stub2 = StubClient(two_factor=True)
    async with make_http(stub2, root) as http:
        await http.post("/api/auth/phone", json={"phone": "+15551234567"})
        res = await http.post("/api/auth/code", json={"code": "12345"})
        check("code leads to the password step", (await res.json())["step"], "password")
        check("status agrees", (await status(http))["step"], "password")
        check("still not authorized", (await http.get("/api/chats")).status, 401)

        bad = await http.post("/api/auth/password", json={"password": "wrong"})
        check("wrong password rejected", bad.status, 400)
        check("message is human", await bad.text(), "that password isn't right")
        check("empty password rejected",
              (await http.post("/api/auth/password", json={"password": ""})).status, 400)
        check("still at the password step", (await status(http))["step"], "password")

        good = await http.post("/api/auth/password", json={"password": "hunter2"})
        check("correct password signs in", (await good.json())["step"], "ready")
        check("data endpoints open up", (await http.get("/api/chats")).status, 200)

    print("\nrequest guard")
    stub3 = StubClient()
    stub3.authorized = True
    async with make_http(stub3, root) as http:
        check("forged Host refused",
              (await http.get("/api/auth/status", headers={"Host": "evil.example"})).status, 403)
        check("cross-origin POST refused",
              (await http.post("/api/auth/phone", json={"phone": "+1555"},
                               headers={"Origin": "https://evil.example"})).status, 403)
        check("same-origin allowed",
              (await http.get("/api/auth/status",
                              headers={"Origin": "http://127.0.0.1:8420"})).status, 200)
        check("no Origin header allowed (curl, the page itself)",
              (await http.get("/api/auth/status")).status, 200)

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
