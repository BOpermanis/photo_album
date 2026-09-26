import platform
import socket
import subprocess
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, func, select

import json

from . import detect
from . import recognize
from . import warp
from .config import FRONTEND_DIR, LIBRARY_DIR, PORT
from .db import get_session, init_db
from .models import Face, Person, Photo
from .upload import router as upload_router


def _is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def _windows_host_ip() -> Optional[str]:
    """Under WSL, the phone must reach the Windows host's LAN IP, not the WSL NAT IP."""
    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object "
                "{$_.InterfaceAlias -notmatch 'WSL|Loopback|Default Switch|Tailscale' "
                "-and $_.IPAddress -notlike '169.*' -and $_.IPAddress -notlike '127.*'} "
                "| Select-Object -First 1).IPAddress",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ip = out.stdout.strip()
        return ip or None
    except Exception:
        return None


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _print_qr() -> None:
    host_ip = _windows_host_ip() if _is_wsl() else None
    ip = host_ip or _lan_ip()
    url = f"http://{ip}:{PORT}"
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print(f"\nOpen on your phone (same Wi-Fi): {url}\n")
    if host_ip:
        print(
            "Detected WSL: your phone must reach the Windows host above.\n"
            "If it doesn't load, run scripts/wsl-forward.ps1 in an admin PowerShell once.\n"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recognize.load()
    detect.start_worker()
    _print_qr()
    yield


app = FastAPI(title="Family Photo Face-Tagging", lifespan=lifespan)
app.include_router(upload_router)


# ----------------------------- serialization -----------------------------
def _face_dict(session: Session, face: Face) -> dict:
    person_name = None
    if face.person_id is not None:
        person = session.get(Person, face.person_id)
        person_name = person.name if person else None
    return {
        "id": face.id,
        "x": face.x,
        "y": face.y,
        "w": face.w,
        "h": face.h,
        "confidence": face.confidence,
        "person_id": face.person_id,
        "person_name": person_name,
    }


def _photo_summary(session: Session, photo: Photo) -> dict:
    faces = session.exec(select(Face).where(Face.photo_id == photo.id)).all()
    untagged = sum(1 for f in faces if f.person_id is None)
    return {
        "id": photo.id,
        "filename": photo.filename,
        "display_filename": photo.display_filename,
        "width": photo.width,
        "height": photo.height,
        "processed": photo.processed,
        "face_count": len(faces),
        "untagged_count": untagged,
    }


def _attach_suggestions(
    session: Session, photo: Photo, faces: list, face_dicts: list
) -> None:
    """Add suggested_person_* fields to unnamed faces that match a known person."""
    unnamed = [f for f in faces if f.person_id is None]
    if not unnamed:
        return
    missing = [f for f in unnamed if not recognize.cache_has(f.id)]
    if missing:
        try:
            embs = detect.get_detector().embed_photo(
                LIBRARY_DIR / photo.display_filename, missing
            )
            for fid, emb in embs.items():
                recognize.cache_set(fid, emb)
        except Exception as exc:  # pragma: no cover - detection is best-effort
            print(f"[recognize] embed failed for {photo.filename}: {exc}")
    by_id = {f.id: d for f, d in zip(faces, face_dicts)}
    for f in unnamed:
        emb = recognize.cache_get(f.id)
        if emb is None:
            continue
        match = recognize.query(emb)
        if not match:
            continue
        person = session.get(Person, match["person_id"])
        if not person:
            continue
        d = by_id[f.id]
        d["suggested_person_id"] = match["person_id"]
        d["suggested_person_name"] = person.name
        d["suggested_score"] = match["score"]
@app.get("/media/{filename}")
def media(filename: str):
    library = LIBRARY_DIR.resolve()
    path = (library / filename).resolve()
    if library not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


# ------------------------------- photos -----------------------------------
@app.get("/api/photos")
def list_photos(filter: str = "all", session: Session = Depends(get_session)):
    photos = session.exec(select(Photo).order_by(Photo.imported_at.desc())).all()
    summaries = [_photo_summary(session, p) for p in photos]
    if filter == "untagged":
        summaries = [
            s for s in summaries if (not s["processed"]) or s["untagged_count"] > 0
        ]
    return {"photos": summaries}


@app.get("/api/photos/{photo_id}")
def get_photo(photo_id: int, session: Session = Depends(get_session)):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    faces = session.exec(select(Face).where(Face.photo_id == photo_id)).all()
    face_dicts = [_face_dict(session, f) for f in faces]
    _attach_suggestions(session, photo, faces, face_dicts)
    return {
        "id": photo.id,
        "filename": photo.filename,
        "display_filename": photo.display_filename,
        "width": photo.width,
        "height": photo.height,
        "processed": photo.processed,
        "description": photo.description,
        "has_edit": bool(photo.edited_filename),
        "faces": face_dicts,
    }


class DescriptionBody(BaseModel):
    description: str


@app.post("/api/photos/{photo_id}/description")
def update_description(
    photo_id: int, body: DescriptionBody, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    photo.description = body.description.strip()
    session.add(photo)
    session.commit()
    return {"id": photo.id, "description": photo.description}


# --------------------------- perspective correction -----------------------
def _reset_photo_faces(session: Session, photo: Photo) -> list[int]:
    """Drop existing detections so the worker re-processes the current image.

    Returns the removed face ids; the caller must drop them from the
    recognition index *after* committing, since that opens its own DB
    connection and would otherwise deadlock on SQLite's write lock.
    """
    faces = session.exec(select(Face).where(Face.photo_id == photo.id)).all()
    ids = [f.id for f in faces]
    for f in faces:
        session.delete(f)
    photo.processed = False
    return ids


@app.get("/api/photos/{photo_id}/adjust")
def adjust_info(photo_id: int, session: Session = Depends(get_session)):
    """Original image dimensions plus a best-guess quad for the corner editor."""
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    original = LIBRARY_DIR / photo.filename
    try:
        suggestion = warp.auto_detect_quad(original)
    except Exception as exc:  # pragma: no cover - detection is best-effort
        print(f"[warp] auto-detect failed for {photo.filename}: {exc}")
        suggestion = None
    if suggestion is None:
        suggestion = [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]]

    with warp._load_rgb(original) as im:  # noqa: SLF001 - internal helper reuse
        ow, oh = im.size

    current = None
    if photo.edit_params:
        try:
            current = json.loads(photo.edit_params)
        except Exception:
            current = None

    return {
        "original_width": ow,
        "original_height": oh,
        "suggestion": suggestion,
        "current": current,
        "has_edit": bool(photo.edited_filename),
        "filename": photo.filename,
    }


class WarpBody(BaseModel):
    rotation: int = 0
    points: List[List[float]]


@app.post("/api/photos/{photo_id}/warp")
def warp_photo(
    photo_id: int, body: WarpBody, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    if len(body.points) != 4:
        raise HTTPException(status_code=400, detail="Exactly 4 points required")
    rotation = int(body.rotation) % 360
    if rotation not in (0, 90, 180, 270):
        raise HTTPException(status_code=400, detail="Rotation must be a multiple of 90")

    try:
        stored_name, w, h = warp.warp_and_save(
            LIBRARY_DIR / photo.filename, rotation, body.points
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Warp failed: {exc}")

    old_edited = photo.edited_filename
    photo.edited_filename = stored_name
    photo.width, photo.height = w, h
    photo.edit_params = json.dumps({"rotation": rotation, "points": body.points})
    removed = _reset_photo_faces(session, photo)
    session.add(photo)
    session.commit()
    for fid in removed:
        recognize.remove(fid)
    if old_edited and old_edited != stored_name:
        warp.delete_file(old_edited)
    return {"id": photo.id, "display_filename": photo.display_filename}


@app.post("/api/photos/{photo_id}/warp/reset")
def reset_warp(photo_id: int, session: Session = Depends(get_session)):
    """Discard the corrected copy and go back to the original upload."""
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    if not photo.edited_filename:
        return {"id": photo.id, "display_filename": photo.display_filename}

    old_edited = photo.edited_filename
    with warp._load_rgb(LIBRARY_DIR / photo.filename) as im:  # noqa: SLF001
        photo.width, photo.height = im.size
    photo.edited_filename = ""
    photo.edit_params = ""
    removed = _reset_photo_faces(session, photo)
    session.add(photo)
    session.commit()
    for fid in removed:
        recognize.remove(fid)
    warp.delete_file(old_edited)
    return {"id": photo.id, "display_filename": photo.display_filename}


# ------------------------------- faces ------------------------------------
class AssignBody(BaseModel):
    name: Optional[str] = None
    person_id: Optional[int] = None


@app.post("/api/faces/{face_id}/assign")
def assign_face(
    face_id: int, body: AssignBody, session: Session = Depends(get_session)
):
    face = session.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    person: Optional[Person] = None
    if body.person_id is not None:
        person = session.get(Person, body.person_id)
        if not person:
            raise HTTPException(status_code=404, detail="Person not found")
    elif body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        person = session.exec(select(Person).where(Person.name == name)).first()
        if not person:
            person = Person(name=name)
            session.add(person)
            session.commit()
            session.refresh(person)
    else:
        raise HTTPException(status_code=400, detail="Provide name or person_id")

    face.person_id = person.id
    session.add(face)
    session.commit()
    _index_reference_face(session, face, person.id)
    return _face_dict(session, face)


def _index_reference_face(session: Session, face: Face, person_id: int) -> None:
    """Add a confirmed face's embedding to the in-memory recognition index."""
    emb = recognize.cache_get(face.id)
    if emb is None:
        photo = session.get(Photo, face.photo_id)
        if photo:
            try:
                embs = detect.get_detector().embed_photo(
                    LIBRARY_DIR / photo.display_filename, [face]
                )
                emb = embs.get(face.id)
                if emb is not None:
                    recognize.cache_set(face.id, emb)
            except Exception as exc:  # pragma: no cover - detection is best-effort
                print(f"[recognize] embed failed for face {face.id}: {exc}")
    if emb is not None:
        recognize.add(face.id, person_id, emb)


@app.post("/api/faces/{face_id}/unassign")
def unassign_face(face_id: int, session: Session = Depends(get_session)):
    face = session.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    face.person_id = None
    session.add(face)
    session.commit()
    recognize.remove(face_id)
    return _face_dict(session, face)


@app.delete("/api/faces/{face_id}")
def delete_face(face_id: int, session: Session = Depends(get_session)):
    face = session.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    session.delete(face)
    session.commit()
    recognize.remove(face_id)
    return {"status": "deleted", "id": face_id}


class ManualFaceBody(BaseModel):
    x: float
    y: float
    w: float
    h: float


@app.post("/api/photos/{photo_id}/faces")
def add_manual_face(
    photo_id: int, body: ManualFaceBody, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    x = max(0.0, min(body.x, float(photo.width)))
    y = max(0.0, min(body.y, float(photo.height)))
    w = min(body.w, float(photo.width) - x)
    h = min(body.h, float(photo.height) - y)
    if w < 1.0 or h < 1.0:
        raise HTTPException(status_code=400, detail="Box is too small")
    face = Face(photo_id=photo_id, x=x, y=y, w=w, h=h, confidence=1.0)
    session.add(face)
    session.commit()
    session.refresh(face)
    return _face_dict(session, face)


# ------------------------------- people -----------------------------------
class PersonBody(BaseModel):
    name: str


@app.get("/api/persons")
def list_persons(session: Session = Depends(get_session)):
    persons = session.exec(select(Person).order_by(Person.name)).all()
    out = []
    for p in persons:
        count = session.exec(
            select(func.count(func.distinct(Face.photo_id))).where(
                Face.person_id == p.id
            )
        ).one()
        out.append({"id": p.id, "name": p.name, "photo_count": count})
    return {"persons": out}


@app.post("/api/persons")
def create_person(body: PersonBody, session: Session = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    existing = session.exec(select(Person).where(Person.name == name)).first()
    if existing:
        return {"id": existing.id, "name": existing.name}
    person = Person(name=name)
    session.add(person)
    session.commit()
    session.refresh(person)
    return {"id": person.id, "name": person.name}


@app.get("/api/persons/{person_id}/photos")
def person_photos(person_id: int, session: Session = Depends(get_session)):
    person = session.get(Person, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    photo_ids = session.exec(
        select(Face.photo_id).where(Face.person_id == person_id).distinct()
    ).all()
    photos = [session.get(Photo, pid) for pid in photo_ids]
    summaries = [_photo_summary(session, p) for p in photos if p]
    return {"person": {"id": person.id, "name": person.name}, "photos": summaries}


# ------------------------------- frontend ---------------------------------
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
