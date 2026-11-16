"""Public calibration isolation. No hostname resolution or external target mode."""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Route, WebSocketRoute

from backend.config import ROOT

COOKIE_NAME = "__Host-frame_session"
PUBLIC_BROWSER_ARGS = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-pings",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
]


class SessionSigner:
    def __init__(self, secret: str | None, lifetime: int):
        self.key = secret.encode() if secret else secrets.token_bytes(32)
        self.lifetime = lifetime

    def issue(self) -> tuple[str, str]:
        owner = secrets.token_hex(24)
        payload = f"{owner}.{int(time.time())}"
        signature = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return owner, f"{payload}.{signature}"

    def verify(self, token: str | None) -> str | None:
        if not token or len(token) > 160:
            return None
        try:
            owner, issued, signature = token.split(".")
            if len(owner) != 48 or any(c not in "0123456789abcdef" for c in owner):
                return None
            age = time.time() - int(issued)
            if age < 0 or age >= self.lifetime:
                return None
            expected = hmac.new(self.key, f"{owner}.{issued}".encode(), hashlib.sha256).hexdigest()
            return owner if hmac.compare_digest(signature, expected) else None
        except (ValueError, TypeError, UnicodeError):
            return None


class RateLimiter:
    """Bounded, process-local fixed windows; do not trust client-supplied IP headers."""

    def __init__(self, max_keys: int):
        self.max_keys = max_keys
        self.entries: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, limit: int, seconds: int) -> bool:
        now = time.monotonic()
        previous = self.entries.get(key)
        if previous and previous[0] > now:
            expires, used = previous
            if used >= limit:
                return False
            self.entries[key] = expires, used + 1
            return True
        if len(self.entries) >= self.max_keys:
            self.entries = {key: value for key, value in self.entries.items() if value[0] > now}
            if len(self.entries) >= self.max_keys:
                return False
        self.entries[key] = now + seconds, 1
        return True


class SocketLimiter:
    def __init__(self, total: int, per_session: int):
        self.total = total
        self.per_session = per_session
        self.owners: dict[str, int] = defaultdict(int)

    def acquire(self, owner: str) -> bool:
        if sum(self.owners.values()) >= self.total or self.owners.get(owner, 0) >= self.per_session:
            return False
        self.owners[owner] += 1
        return True

    def release(self, owner: str) -> None:
        self.owners[owner] -= 1
        if not self.owners[owner]:
            del self.owners[owner]


def calibration_url(origin: str) -> str:
    return origin.rstrip("/") + "/demo"


def is_calibration_url(url: str, origin: str) -> bool:
    # Exact matching also rejects credentials, ports/hosts supplied in the URL,
    # redirect parameters, fragments, escaped paths and alternate IP spellings.
    return url == calibration_url(origin) and urlsplit(url).scheme in {"http", "https"}


def directory_bytes(root: Path) -> int:
    """Do not follow symlinks into unrelated storage."""
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


async def restrict_public_context(context: BrowserContext, url: str) -> None:
    """Install before new_page; only locally fulfilled top-level calibration loads."""
    if not hasattr(context, "route_web_socket"):
        raise RuntimeError("Public demo requires Playwright WebSocket routing support")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.path != "/demo" or parsed.query or parsed.fragment:
        raise ValueError("Public browser accepts only the calibration document")
    fixture = (ROOT / "tests" / "fixtures" / "layout.html").read_bytes()

    async def route_request(route: Route):
        request = route.request
        if (request.url == url and request.method == "GET" and request.is_navigation_request()
                and request.resource_type == "document" and request.frame.parent_frame is None):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=fixture)
        else:
            await route.abort("blockedbyclient")

    async def close_socket(socket: WebSocketRoute):
        await socket.close(code=1008, reason="Network disabled in public calibration mode")

    await context.route("**/*", route_request)
    await context.route_web_socket("**/*", close_socket)
    await context.clear_permissions()
    await context.add_init_script("""(() => {
        const blocked = () => { throw new Error('Network disabled in public calibration mode'); };
        for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
            Object.defineProperty(globalThis, name, {value: blocked, writable: false, configurable: false});
        }
    })();""")
