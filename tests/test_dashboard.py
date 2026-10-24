from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dashboard import router


def test_dashboard_serves_frontend_and_blocks_unknown_assets() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    index = client.get("/dashboard")
    assert index.status_code == 200
    assert "News Automation Studio" in index.text
    assert "/dashboard/app.js" in index.text
    assert "Review queue" in index.text

    styles = client.get("/dashboard/styles.css")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")

    script = client.get("/dashboard/app.js")
    assert script.status_code == 200
    assert "approval" in script.text.lower()

    assert client.get("/dashboard/missing.js").status_code == 404
    assert client.get("/").status_code == 200


def test_dashboard_redirects_root_to_review_queue() -> None:
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/", follow_redirects=False)
    assert response.status_code in {307, 302}
    assert response.headers["location"] == "/dashboard"
