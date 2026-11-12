from playwright.async_api import Page


class RuntimeCollector:
    def __init__(self, page: Page, limit: int = 100):
        self.bugs: list[dict] = []
        self.warnings: list[str] = []
        self._seen: set[tuple[str, str]] = set()
        self.limit = limit
        page.on("console", lambda msg: self.add("console_error", msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda error: self.add("javascript_error", str(error)))
        page.on("response", lambda response: self.add("http_error", f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
        page.on("requestfailed", lambda request: self.add("request_failed", f"{request.failure}: {request.url}"))

    def add(self, kind: str, message: str) -> None:
        message = message[:2000]
        key = kind, message
        if key in self._seen:
            return
        if len(self.bugs) >= self.limit:
            if not self.warnings:
                self.warnings.append(f"Runtime findings capped at {self.limit} unique events.")
            return
        self._seen.add(key)
        self.bugs.append({
            "type": kind, "pillar": "runtime", "selector": "(page runtime)",
            "severity": "high" if kind == "javascript_error" else "medium",
            "message": message, "rect": None,
            "suggestion": "Inspect the browser console or network request. Correct the failing URL, server response, or JavaScript exception; CSS cannot repair this error.",
        })
