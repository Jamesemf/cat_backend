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

# Uploads are re-encoded to one of these on the way in (see utils/upload.py), so
# we serve them with an explicit image content type and never let the browser
# sniff a stored file into an executable type (e.g. a legacy .html polyglot).
_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@router.get("/uploads/{subpath:path}")
def serve_upload(subpath: str):
    key = f"{UPLOADS_PREFIX}/{subpath}"
    storage = get_storage()

    if isinstance(storage, LocalStorage):
        # _path raises ValueError for keys that escape the uploads root (e.g. a
        # "../../etc/passwd" traversal). Treat that as a plain 404 rather than
        # leaking a 500 — no file is disclosed either way.
        try:
            path = storage._path(key)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found.")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        media_type = _IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(
            path,
            media_type=media_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    if not storage.exists(key):
        raise HTTPException(status_code=404, detail="Not found.")
    return RedirectResponse(url=storage.url(key))
