"""Serve uploaded media at /uploads/* regardless of storage backend.

Local backend: stream the file off disk (what StaticFiles used to do).
S3 backend: 307-redirect to the object's URL (presigned or CDN), so bytes never
flow through the API. In production with a CDN configured the frontend gets the
absolute URL directly from the API responses and never hits this route at all —
it exists mainly for local dev and presigned-S3 fallback.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app.services.storage import UPLOADS_PREFIX, LocalStorage, get_storage

router = APIRouter(tags=["media"])


@router.get("/uploads/{subpath:path}")
def serve_upload(subpath: str):
    key = f"{UPLOADS_PREFIX}/{subpath}"
    storage = get_storage()

    if isinstance(storage, LocalStorage):
        path = storage._path(key)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        return FileResponse(path)

    if not storage.exists(key):
        raise HTTPException(status_code=404, detail="Not found.")
    return RedirectResponse(url=storage.url(key))
