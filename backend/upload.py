import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from PIL import Image, ImageOps
from sqlmodel import Session, select

from . import jobs
from .config import ALLOWED_EXTENSIONS, LIBRARY_DIR
from .db import get_session
from .models import Album, Photo

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass

router = APIRouter()


def extract_taken_at(path: Path) -> Optional[datetime]:
    """Return the EXIF capture time (DateTimeOriginal) as a naive datetime, or None."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
        if not exif:
            return None
        raw = None
        try:
            sub = exif.get_ifd(0x8769)  # Exif sub-IFD holds DateTimeOriginal/Digitized
        except Exception:
            sub = {}
        for tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized
            if sub.get(tag):
                raw = sub[tag]
                break
        if not raw:
            raw = exif.get(306)  # DateTime (IFD0)
        if not raw:
            return None
        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _resolve_album_id(session: Session, album_id: Optional[int]) -> Optional[int]:
    if album_id is not None and session.get(Album, album_id):
        return album_id
    # Fall back to the earliest album so every upload lands somewhere.
    first = session.exec(select(Album).order_by(Album.id)).first()
    return first.id if first else None


@router.post("/api/upload")
async def upload(
    files: List[UploadFile] = File(...),
    album_id: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    target_album = _resolve_album_id(session, album_id)
    results = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            results.append(
                {"filename": file.filename, "status": "skipped", "reason": "unsupported type"}
            )
            continue

        data = await file.read()
        content_hash = hashlib.sha256(data).hexdigest()

        existing = session.exec(
            select(Photo).where(Photo.content_hash == content_hash)
        ).first()
        if existing:
            results.append(
                {"filename": file.filename, "status": "duplicate", "id": existing.id}
            )
            continue

        stored_name = f"{content_hash[:16]}_{uuid.uuid4().hex[:8]}{ext}"
        dest = LIBRARY_DIR / stored_name
        dest.write_bytes(data)

        try:
            with Image.open(dest) as img:
                img = ImageOps.exif_transpose(img)
                width, height = img.size
        except Exception:
            width = height = 0

        photo = Photo(
            album_id=target_album,
            filename=stored_name,
            content_hash=content_hash,
            width=width,
            height=height,
            original_width=width,
            original_height=height,
            taken_at=extract_taken_at(dest),
        )
        session.add(photo)
        session.commit()
        session.refresh(photo)
        jobs.enqueue(session, photo.id, "detect")
        results.append({"filename": file.filename, "status": "uploaded", "id": photo.id})

    return {"results": results}
