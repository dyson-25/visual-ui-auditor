from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class AuditRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid URL or port") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Use a complete http:// or https:// URL")
        if parsed.username or parsed.password or any(ord(c) < 33 for c in value):
            raise ValueError("Credentials and whitespace are not allowed in URLs")
        if port == 0:
            raise ValueError("Port must be between 1 and 65535")
        return value


class Rect(BaseModel):
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class BugItem(BaseModel):
    id: str = ""
    number: int | None = Field(default=None, ge=1)
    type: str
    pillar: Literal["overflow", "collision", "touch", "runtime", "accessibility"]
    selector: str
    severity: Literal["high", "medium"]
    message: str
    rect: Rect | None = None
    related_selector: str | None = None
    suggestion: str
    help_url: str | None = None


class ViewportReport(BaseModel):
    name: str
    width: int
    height: int
    screenshot_width: int
    screenshot_height: int
    screenshot: str
    heatmap: str
    bugs: list[BugItem]
    warnings: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
