from playwright.async_api import ConsoleMessage, Page


class RuntimeCollector:
    def __init__(self, page: Page, limit: int = 100):
        self.bugs: list[dict] = []
        self.warnings: list[str] = []
        self._seen: set[tuple[str, str]] = set()
        self.limit = limit
        page.on("console", self.console_error)
        page.on("pageerror", lambda error: self.add("javascript_error", error.stack or str(error)))
        page.on("response", lambda response: self.add("http_error", f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
        page.on("requestfailed", lambda request: self.add("request_failed", f"{request.failure}: {request.url}"))

    def console_error(self, message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        location = message.location
        source = location.get("url", "")
        position = f"{source}:{location.get('lineNumber', 0) + 1}:{location.get('columnNumber', 0) + 1}" if source else ""
        self.add("console_error", f"{position}\n{message.text}" if position else message.text)

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
