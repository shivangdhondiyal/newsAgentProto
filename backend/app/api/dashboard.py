from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(include_in_schema=False)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"
ASSETS = {
    "app.js": ("app.js", "application/javascript"),
    "styles.css": ("styles.css", "text/css"),
}


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    index = FRONTEND_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard frontend is not installed")
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/dashboard/{asset_name}", include_in_schema=False)
async def dashboard_asset(asset_name: str) -> FileResponse:
    asset = ASSETS.get(asset_name)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    filename, media_type = asset
    path = FRONTEND_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dashboard asset is not installed")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
