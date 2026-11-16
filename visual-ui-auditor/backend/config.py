import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
STORAGE = Path(os.environ.get("AUDITOR_STORAGE", str(ROOT / "backend" / "storage")))
SCREENSHOTS = STORAGE / "screenshots"
NAVIGATION_TIMEOUT_MS = max(1000, int(os.environ.get("NAVIGATION_TIMEOUT_MS", "30000")))
AUDIT_TIMEOUT_SECONDS = max(10, int(os.environ.get("AUDIT_TIMEOUT_SECONDS", "180")))
MAX_PAGE_HEIGHT = max(1024, min(30000, int(os.environ.get("MAX_PAGE_HEIGHT", "16000"))))
MAX_CONCURRENT_AUDITS = max(1, min(4, int(os.environ.get("MAX_CONCURRENT_AUDITS", "2"))))


@dataclass(frozen=True)
class SecurityConfig:
    """Explicit factory/test policy; the deployed app uses from_env, safe by default."""

    public_demo: bool = False
    session_secret: str | None = None
    public_origin: str | None = None
    session_seconds: int = 604800
    retention_seconds: int = 86400
    cleanup_interval_seconds: int = 60
    max_url_length: int = 2048
    max_active_jobs: int = 1
    max_pending_jobs: int = 4
    submissions_per_session: int = 5
    submissions_per_ip: int = 20
    rate_window_seconds: int = 3600
    requests_per_minute: int = 120
    max_rate_keys: int = 4096
    max_public_audits: int = 100
    max_storage_bytes: int = 256 * 1024 * 1024
    max_audit_bytes: int = 16 * 1024 * 1024
    max_report_bytes: int = 2 * 1024 * 1024
    max_websockets: int = 32
    max_session_websockets: int = 4

    def __post_init__(self):
        for name, value in vars(self).items():
            if name not in {"public_demo", "session_secret", "public_origin"} and (not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.session_secret is not None and len(self.session_secret.encode()) < 32:
            raise ValueError("AUDITOR_SESSION_SECRET must contain at least 32 bytes")
        if self.public_origin is not None:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or parsed.port == 0 or any(ord(c) <= 32 for c in self.public_origin)):
                raise ValueError("PUBLIC_ORIGIN must be a complete HTTPS origin without credentials, path, query or fragment")
            object.__setattr__(self, "public_origin", self.public_origin.rstrip("/"))

    @classmethod
    def from_env(cls) -> "SecurityConfig":
        # Only an explicit false value enables trusted local/private-network targets.
        public = os.environ.get("PUBLIC_DEMO_MODE", "true").lower().strip() not in {"0", "false", "no"}
        return cls(
            public_demo=public,
            session_secret=os.environ.get("AUDITOR_SESSION_SECRET") or None,
            public_origin=(os.environ.get("PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL") or None) if public else None,
            retention_seconds=max(60, int(os.environ.get("PUBLIC_AUDIT_RETENTION_SECONDS", "86400"))),
            max_storage_bytes=max(16 * 1024 * 1024, int(os.environ.get("PUBLIC_STORAGE_MAX_MB", "256")) * 1024 * 1024),
        )
