import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
STORAGE = Path(os.environ.get("AUDITOR_STORAGE", str(ROOT / "backend" / "storage")))
SCREENSHOTS = STORAGE / "screenshots"
NAVIGATION_TIMEOUT_MS = max(1000, int(os.environ.get("NAVIGATION_TIMEOUT_MS", "30000")))
AUDIT_TIMEOUT_SECONDS = max(10, int(os.environ.get("AUDIT_TIMEOUT_SECONDS", "180")))
MAX_PAGE_HEIGHT = max(1024, min(30000, int(os.environ.get("MAX_PAGE_HEIGHT", "16000"))))
MAX_CONCURRENT_AUDITS = max(1, min(4, int(os.environ.get("MAX_CONCURRENT_AUDITS", "2"))))
