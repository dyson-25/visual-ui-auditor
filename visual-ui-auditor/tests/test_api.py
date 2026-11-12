from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

HEADERS = {"X-Audit-Client": "dashboard"}


def test_api_live_audit_replay_and_exports(tmp_path, fixture_url):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/audit", json={"url": fixture_url}, headers=HEADERS)
        assert response.status_code == 202
        audit_id = response.json()["audit_id"]
        messages = []
        with client.websocket_connect(f"/ws/audit/{audit_id}") as ws:
            while True:
                event = ws.receive_json()
                if event.get("heartbeat"):
                    continue
                messages.append(event)
                if event["status"] in {"completed", "failed"}:
                    break
        assert messages[-1]["status"] == "completed", messages[-1]
        assert len(messages) >= 10
        assert messages[0]["status"] == "queued"
        audit = client.get(f"/api/audit/{audit_id}").json()
        assert len(audit["report"]["viewports"]) == 3
        assert client.get(audit["report"]["viewports"][0]["screenshot"]).status_code == 200
        assert client.get("/api/audits").json()[0]["id"] == audit_id
        with client.websocket_connect(f"/ws/audit/{audit_id}?after={messages[-2]['seq']}") as ws:
            assert ws.receive_json()["status"] == "completed"
        with client.websocket_connect(f"/ws/audit/{audit_id}?after={messages[-1]['seq']}") as ws:
            assert ws.receive_json()["status"] == "completed"
        json_export = client.get(f"/api/audit/{audit_id}/export/json")
        assert json_export.json() == audit
        html = client.get(f"/api/audit/{audit_id}/export/html")
        assert "data:image/png;base64," in html.text
        assert "<script" not in html.text
        pdf = client.get(f"/api/audit/{audit_id}/export/pdf")
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
        assert client.get(f"/api/audit/{audit_id}/export/nope").status_code == 404
    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get(f"/api/audit/{audit_id}").json()["status"] == "completed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com", "http://localhost:0", "http://localhost:bad", "https://example.com/ bad"])
def test_invalid_urls(tmp_path, url):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/audit", json={"url": url}, headers=HEADERS).status_code == 422


def test_local_origin_and_missing_audit(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/audit", json={"url": "http://localhost"}).status_code == 403
        assert client.post("/api/audit", json={"url": "http://localhost"}, headers={**HEADERS, "Origin": "https://other.example"}).status_code == 403
        assert client.get(f"/api/audit/{uuid4()}").status_code == 404
        assert client.get("/api/audits", headers={"Host": "untrusted.example"}).status_code == 400


def test_failure_is_streamed_and_export_rejected(tmp_path, monkeypatch):
    async def broken(*args):
        raise RuntimeError("Controlled failure")

    monkeypatch.setattr("backend.main.run_audit", broken)
    with TestClient(create_app(tmp_path)) as client:
        audit_id = client.post("/api/audit", json={"url": "http://localhost"}, headers=HEADERS).json()["audit_id"]
        with client.websocket_connect(f"/ws/audit/{audit_id}") as ws:
            while True:
                event = ws.receive_json()
                if event["status"] == "failed":
                    assert "Controlled failure" in event["message"]
                    break
        assert client.get(f"/api/audit/{audit_id}/export/html").status_code == 409
