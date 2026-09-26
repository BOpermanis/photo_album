import json
import platform
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import quote

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, func, select

from . import export, jobs, recognize, warp, worker
from .config import FRONTEND_DIR, LIBRARY_DIR, PORT
from .db import engine, get_session, init_db
from .models import Album, Face, Job, Person, Photo
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
    with Session(engine) as session:
        requeued = jobs.requeue_stale(session)
        if requeued:
            print(f"[jobs] requeued {requeued} stale job(s)")
        cleared = jobs.clear_done(session)
        if cleared:
            print(f"[jobs] cleared {cleared} finished job(s)")
    app.state.worker_pool = worker.start_pool()
    _print_qr()
    yield
    worker.stop_pool(getattr(app.state, "worker_pool", None))


app = FastAPI(title="Family Photo Face-Tagging", lifespan=lifespan)
app.include_router(upload_router)


# ----------------------------- serialization -----------------------------
def _parse_poly(raw: str):
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _face_embedding(face: Face):
    if not face.embedding:
        return None
    return np.frombuffer(face.embedding, dtype=np.float32)


def _forward_matrix(photo: Photo):
    """Homography original -> aligned from the photo's transform, or None."""
    if not photo.edit_params or not photo.original_width:
        return None
    params = json.loads(photo.edit_params)
    matrix, _, _ = warp.build_forward_matrix(
        (photo.original_width, photo.original_height),
        params["rotation"],
        params["points"],
    )
    return matrix


def _poly_bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x, y = min(xs), min(ys)
    return x, y, max(xs) - x, max(ys) - y


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
        "poly_original": _parse_poly(face.poly_original),
        "poly_aligned": _parse_poly(face.poly_aligned),
        "confidence": face.confidence,
        "person_id": face.person_id,
        "person_name": person_name,
    }


def _photo_summary(session: Session, photo: Photo) -> dict:
    faces = session.exec(select(Face).where(Face.photo_id == photo.id)).all()
    untagged = sum(1 for f in faces if f.person_id is None)
    flags = jobs.pending_flags(session, photo.id)
    return {
        "id": photo.id,
        "album_id": photo.album_id,
        "filename": photo.filename,
        "display_filename": photo.display_filename,
        "width": photo.width,
        "height": photo.height,
        "processed": photo.processed,
        "face_count": len(faces),
        "untagged_count": untagged,
        "detect_pending": flags["detect_pending"],
        "align_pending": flags["align_pending"],
    }


def _attach_suggestions(
    session: Session, photo: Photo, faces: list, face_dicts: list
) -> None:
    """Add suggested_person_* fields to unnamed faces that match a known person."""
    by_id = {f.id: d for f, d in zip(faces, face_dicts)}
    for f in faces:
        if f.person_id is not None:
            continue
        emb = _face_embedding(f)
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
def list_photos(
    filter: str = "all",
    album_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    query = select(Photo).order_by(Photo.imported_at.desc())
    if album_id is not None:
        query = query.where(Photo.album_id == album_id)
    photos = session.exec(query).all()
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
    return _photo_detail(session, photo)


def _photo_detail(session: Session, photo: Photo) -> dict:
    faces = session.exec(select(Face).where(Face.photo_id == photo.id)).all()
    face_dicts = [_face_dict(session, f) for f in faces]
    _attach_suggestions(session, photo, faces, face_dicts)
    flags = jobs.pending_flags(session, photo.id)
    return {
        "id": photo.id,
        "album_id": photo.album_id,
        "filename": photo.filename,
        "display_filename": photo.display_filename,
        "edited_filename": photo.edited_filename,
        "width": photo.width,
        "height": photo.height,
        "original_width": photo.original_width or photo.width,
        "original_height": photo.original_height or photo.height,
        "processed": photo.processed,
        "description": photo.description,
        "has_edit": bool(photo.edited_filename),
        "detect_pending": flags["detect_pending"],
        "align_pending": flags["align_pending"],
        "faces": face_dicts,
    }


def _photo_state_sig(session: Session, photo_id: int):
    """Cheap fingerprint of the fields the detail view reacts to, or None."""
    photo = session.get(Photo, photo_id)
    if photo is None:
        return None
    flags = jobs.pending_flags(session, photo_id)
    face_count = session.exec(
        select(func.count()).select_from(Face).where(Face.photo_id == photo_id)
    ).one()
    return (
        photo.processed,
        flags["detect_pending"],
        flags["align_pending"],
        photo.edited_filename or "",
        photo.width,
        photo.height,
        int(face_count),
    )


@app.get("/api/photos/{photo_id}/stream")
def stream_photo(photo_id: int):
    """Server-sent events: push the photo detail whenever background work
    (align/detect) changes its state, so the client never has to poll.

    Runs as a sync generator, so Starlette iterates it in a threadpool and the
    `time.sleep` never blocks the event loop.
    """

    def gen():
        last = object()
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            with Session(engine) as session:
                sig = _photo_state_sig(session, photo_id)
                if sig is None:
                    yield "event: gone\ndata: {}\n\n"
                    return
                if sig != last:
                    last = sig
                    detail = _photo_detail(session, session.get(Photo, photo_id))
                    yield f"data: {json.dumps(detail)}\n\n"
                    still_pending = sig[1] or sig[2] or not sig[0]
                    if not still_pending:
                        yield "event: done\ndata: {}\n\n"
                        return
            time.sleep(0.15)
        yield "event: timeout\ndata: {}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        gen(), media_type="text/event-stream", headers=headers
    )


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


def _purge_photo(session: Session, photo: Photo) -> tuple[list[int], list[str]]:
    """Delete a photo's faces, queued jobs and photo row (without committing).

    Returns the reference-face ids to drop from the recognition index and the
    image files to unlink; the caller commits, then applies both side effects.
    """
    faces = session.exec(select(Face).where(Face.photo_id == photo.id)).all()
    ref_ids = [f.id for f in faces if f.person_id is not None]
    for f in faces:
        session.delete(f)
    for job in session.exec(select(Job).where(Job.photo_id == photo.id)).all():
        session.delete(job)
    files = [name for name in (photo.filename, photo.edited_filename) if name]
    session.delete(photo)
    return ref_ids, files


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, session: Session = Depends(get_session)):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    ref_ids, files = _purge_photo(session, photo)
    session.commit()
    for face_id in ref_ids:
        recognize.remove(face_id)
    for name in files:
        warp.delete_file(name)
    return {"status": "deleted", "id": photo_id}


# --------------------------- perspective correction -----------------------
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
    detected = suggestion is not None
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
        "detected": detected,
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

    # Save the transform and let a background job build the aligned image and
    # remap face polygons. The request returns immediately so the UI stays snappy.
    photo.edit_params = json.dumps({"rotation": rotation, "points": body.points})
    session.add(photo)
    session.commit()
    jobs.enqueue(session, photo.id, "align")
    return {"id": photo.id, "status": "queued"}


@app.post("/api/photos/{photo_id}/warp/reset")
def reset_warp(photo_id: int, session: Session = Depends(get_session)):
    """Discard the corrected copy and go back to the original upload."""
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    old_edited = photo.edited_filename
    photo.edit_params = ""
    if old_edited:
        photo.edited_filename = ""
        photo.width = photo.original_width or photo.width
        photo.height = photo.original_height or photo.height
        for f in session.exec(select(Face).where(Face.photo_id == photo_id)).all():
            if f.poly_aligned:
                f.poly_aligned = ""
                session.add(f)
    session.add(photo)
    session.commit()
    if old_edited:
        warp.delete_file(old_edited)
    return {"id": photo.id, "display_filename": photo.display_filename}


@app.post("/api/photos/{photo_id}/rerun-detection")
def rerun_detection(photo_id: int, session: Session = Depends(get_session)):
    """Queue a fresh detection on the current (aligned) image."""
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    jobs.enqueue(session, photo.id, "rerun_detect")
    return {"id": photo.id, "status": "queued"}


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
    emb = _face_embedding(face)
    if emb is not None:
        recognize.add(face.id, person_id, emb)
    else:
        # Manual boxes have no embedding until a detection run computes one.
        print(f"[recognize] no embedding for face {face.id}; skipping index")


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
    # Which image the coordinates are in: "original", "aligned", or "auto".
    space: str = "auto"


@app.post("/api/photos/{photo_id}/faces")
def add_manual_face(
    photo_id: int, body: ManualFaceBody, session: Session = Depends(get_session)
):
    photo = session.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    space = body.space
    if space == "auto":
        space = "aligned" if photo.edited_filename else "original"
    aligned = space == "aligned" and bool(photo.edited_filename)

    if aligned:
        max_w, max_h = float(photo.width), float(photo.height)
    else:
        max_w = float(photo.original_width or photo.width)
        max_h = float(photo.original_height or photo.height)
    x = max(0.0, min(body.x, max_w))
    y = max(0.0, min(body.y, max_h))
    w = min(body.w, max_w - x)
    h = min(body.h, max_h - y)
    if w < 1.0 or h < 1.0:
        raise HTTPException(status_code=400, detail="Box is too small")

    rect = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    matrix = _forward_matrix(photo)
    if aligned:
        poly_aligned = rect
        poly_original = (
            warp.map_points(np.linalg.inv(matrix), rect) if matrix is not None else rect
        )
        bx, by, bw, bh = _poly_bbox(poly_original)
    else:
        poly_original = rect
        poly_aligned = warp.map_points(matrix, rect) if matrix is not None else None
        bx, by, bw, bh = x, y, w, h

    face = Face(
        photo_id=photo_id,
        x=bx, y=by, w=bw, h=bh,
        confidence=1.0,
        poly_original=json.dumps(poly_original),
        poly_aligned=json.dumps(poly_aligned) if poly_aligned is not None else "",
    )
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


# ------------------------------- albums -----------------------------------
class AlbumBody(BaseModel):
    name: Optional[str] = None


def _album_stats(session: Session, album_id: int) -> dict:
    image_count = session.exec(
        select(func.count(Photo.id)).where(Photo.album_id == album_id)
    ).one()
    face_count = session.exec(
        select(func.count(Face.id))
        .select_from(Face)
        .join(Photo, Face.photo_id == Photo.id)
        .where(Photo.album_id == album_id)
    ).one()
    identity_count = session.exec(
        select(func.count(func.distinct(Face.person_id)))
        .select_from(Face)
        .join(Photo, Face.photo_id == Photo.id)
        .where(Photo.album_id == album_id, Face.person_id.is_not(None))
    ).one()
    return {
        "image_count": image_count,
        "face_count": face_count,
        "identity_count": identity_count,
    }


def _next_album_name(session: Session) -> str:
    n = session.exec(select(func.count(Album.id))).one() + 1
    name = f"Album_{n}"
    while session.exec(select(Album).where(Album.name == name)).first():
        n += 1
        name = f"Album_{n}"
    return name


@app.get("/api/albums")
def list_albums(session: Session = Depends(get_session)):
    albums = session.exec(select(Album).order_by(Album.id)).all()
    out = [
        {"id": a.id, "name": a.name, **_album_stats(session, a.id)} for a in albums
    ]
    return {"albums": out}


@app.post("/api/albums")
def create_album(
    body: Optional[AlbumBody] = None, session: Session = Depends(get_session)
):
    name = (body.name or "").strip() if body else ""
    if not name:
        name = _next_album_name(session)
    if session.exec(select(Album).where(Album.name == name)).first():
        raise HTTPException(status_code=400, detail="Album name already exists")
    album = Album(name=name)
    session.add(album)
    session.commit()
    session.refresh(album)
    return {"id": album.id, "name": album.name}


@app.patch("/api/albums/{album_id}")
def rename_album(
    album_id: int, body: AlbumBody, session: Session = Depends(get_session)
):
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    dup = session.exec(
        select(Album).where(Album.name == name, Album.id != album_id)
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail="Album name already exists")
    album.name = name
    session.add(album)
    session.commit()
    return {"id": album.id, "name": album.name}


@app.delete("/api/albums/{album_id}")
def delete_album(album_id: int, session: Session = Depends(get_session)):
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    ref_ids: list[int] = []
    files: list[str] = []
    for photo in session.exec(select(Photo).where(Photo.album_id == album_id)).all():
        photo_refs, photo_files = _purge_photo(session, photo)
        ref_ids += photo_refs
        files += photo_files
    session.delete(album)
    session.commit()
    for face_id in ref_ids:
        recognize.remove(face_id)
    for name in files:
        warp.delete_file(name)
    return {"status": "deleted", "id": album_id}


@app.get("/api/albums/{album_id}/export")
def export_album(album_id: int, session: Session = Depends(get_session)):
    album = session.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    pdf = export.build_album_pdf(session, album)
    # HTTP headers are latin-1; keep an ASCII fallback and hand the real UTF-8
    # (e.g. Latvian) name via RFC 5987 filename* for modern browsers.
    ascii_name = "".join(
        c if c.isascii() and (c.isalnum() or c in " -_") else "_" for c in album.name
    ).strip()
    fallback = f"{ascii_name or 'album'}.pdf"
    utf8_name = quote(f"{album.name}.pdf")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{utf8_name}"
        )
    }
    return StreamingResponse(pdf, media_type="application/pdf", headers=headers)


# ------------------------------- jobs -------------------------------------
@app.get("/api/jobs")
def list_jobs(session: Session = Depends(get_session)):
    rows = session.exec(select(Job).order_by(Job.id.desc())).all()
    out = []
    for j in rows:
        photo = session.get(Photo, j.photo_id)
        out.append(
            {
                "id": j.id,
                "photo_id": j.photo_id,
                "display_filename": photo.display_filename if photo else None,
                "kind": j.kind,
                "status": j.status,
                "priority": j.priority,
                "error": j.error,
                "created_at": j.created_at,
                "started_at": j.started_at,
                "updated_at": j.updated_at,
            }
        )
    return {"jobs": out}


# ------------------------------- frontend ---------------------------------
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
