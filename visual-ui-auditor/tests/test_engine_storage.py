import json
from pathlib import Path

from PIL import Image, ImageChops

from backend.database import Database
from backend.engine.orchestrator import run_audit


async def test_complete_audit_persists_heatmaps_and_events(tmp_path, fixture_url):
    db = Database(tmp_path / "audits.db")
    await db.initialize()
    await db.create("test", fixture_url)

    async def progress(value, message):
        await db.event("test", "running", value, message)

    report = await run_audit(fixture_url, "test", tmp_path / "screenshots", progress)
    await db.event("test", "completed", 100, "Done", report)
    saved = await db.get("test")
    assert saved["report"] == report
    assert saved["status"] == "completed"
    assert report["summary"]["total"] > 0
    numbers = [b["number"] for v in report["viewports"] for b in v["bugs"]]
    assert numbers == list(range(1, len(numbers) + 1))
    assert set(report["summary"]["by_pillar"]) == {"overflow", "collision", "touch", "runtime", "accessibility"}
    for viewport in report["viewports"]:
        with Image.open(tmp_path / "screenshots" / Path(viewport["screenshot"]).name) as raw:
            with Image.open(tmp_path / "screenshots" / Path(viewport["heatmap"]).name) as annotated:
                assert raw.width == viewport["width"]
                assert raw.height == viewport["screenshot_height"]
                assert ImageChops.difference(raw.convert("RGB"), annotated).getbbox() is not None
    events = await db.events("test")
    assert events[-1]["status"] == "completed"
    assert [e["progress"] for e in events] == sorted(e["progress"] for e in events)
    assert await db.events("test", events[-1]["seq"]) == []
    assert json.dumps(report)


async def test_restart_marks_interrupted_jobs_failed(tmp_path):
    db = Database(tmp_path / "audits.db")
    await db.initialize()
    await db.create("interrupted", "http://localhost")
    await db.initialize()
    assert (await db.get("interrupted"))["status"] == "failed"
    assert (await db.events("interrupted"))[-1]["status"] == "failed"
    assert await db.get("missing") is None
    assert len(await db.history()) == 1
