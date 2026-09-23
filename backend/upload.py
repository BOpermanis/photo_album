import hashlib
import uuid
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, UploadFile
from PIL import Image, ImageOps
from sqlmodel import Session, select

from .config import ALLOWED_EXTENSIONS, LIBRARY_DIR
from .db import get_session
from .models import Photo

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass

router = APIRouter()


@router.post("/api/upload")
async def upload(
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
):
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
            filename=stored_name,
            content_hash=content_hash,
            width=width,
            height=height,
        )
        session.add(photo)
        session.commit()
        session.refresh(photo)
        results.append({"filename": file.filename, "status": "uploaded", "id": photo.id})

    return {"results": results}
