"""Safe handling of user-uploaded images.

Every photo the app accepts flows through here before it is stored. Two jobs:

* ``read_upload_capped`` reads the body in chunks and aborts the moment it
  passes the size cap, so an oversized (or Content-Length-lying) upload is
  never fully materialised in memory.
* ``sanitize_image`` re-encodes the image through Pillow. This strips EXIF and
  other embedded metadata (crucially the GPS coordinates phones bake into
  photos — e.g. an owner's home in a cat's cover shot) and derives the stored
  file extension from the *decoded* image format, never the client-supplied
  filename. That kills the JPEG/HTML polyglot: a "cat.html" that happens to be
  a valid JPEG is re-encoded to clean JPEG bytes and stored as ``.jpg``, so it
  can never be served from our origin as text/html.
"""

from __future__ import annotations

import io

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

_CHUNK = 64 * 1024

# Decoded Pillow format -> stored extension. Anything not here is rejected, so
# the server only ever stores (and serves) known image types.
_FORMAT_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


async def read_upload_capped(upload: UploadFile, max_bytes: int, detail: str) -> bytes:
    """Read ``upload`` fully, raising 413 as soon as it exceeds ``max_bytes``.

    Reads in fixed chunks and stops one chunk past the cap, so a hostile client
    cannot force an arbitrarily large allocation before the size check runs.
    """
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail=detail)
    return bytes(buf)


def sanitize_image(data: bytes) -> tuple[bytes, str]:
    """Re-encode ``data`` to strip metadata and pin a safe type.

    Returns ``(clean_bytes, extension)``. Raises 400 for anything Pillow cannot
    decode as an allow-listed image format. The returned extension is derived
    from the decoded format, so the caller must NOT use the client filename.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            ext = _FORMAT_EXT.get(fmt)
            if ext is None:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported image type. Please upload a JPEG, PNG, or WebP.",
                )
            img.load()
            out = io.BytesIO()
            # Re-saving without passing an exif/metadata block drops it entirely.
            if fmt == "JPEG":
                img.convert("RGB").save(out, format="JPEG", quality=90)
            elif fmt == "PNG":
                img.save(out, format="PNG")
            else:  # WEBP
                img.save(out, format="WEBP", quality=90)
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="That file isn't a valid image.") from exc
    return out.getvalue(), ext
