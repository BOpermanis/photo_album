"""Render an album to a PDF: a cover page plus one photo per page, each with
its description and the people tagged in it."""

import io
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageOps
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from sqlmodel import Session, select

from .config import LIBRARY_DIR
from .models import Album, Face, Person, Photo

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass


def _register_unicode_fonts() -> tuple[str, str]:
    """Embed a Unicode TTF so non-ASCII text (e.g. Latvian) renders correctly.

    The built-in PDF fonts only cover Latin-1; DejaVu Sans covers the Baltic
    letters. Falls back to Helvetica if no TTF is found on the system.
    """
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if Path(regular).is_file() and Path(bold).is_file():
            try:
                pdfmetrics.registerFont(TTFont("AlbumBody", regular))
                pdfmetrics.registerFont(TTFont("AlbumBold", bold))
                return "AlbumBody", "AlbumBold"
            except Exception:  # pragma: no cover - registration is best-effort
                break
    return "Helvetica", "Helvetica-Bold"


PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
BODY_FONT, BOLD_FONT = _register_unicode_fonts()
BODY_SIZE = 11
LINE_H = 15
# Longest side to embed; letter at ~180 DPI is ample for print and keeps the
# PDF small. Images are re-encoded as JPEG so pages stay a few hundred KB each.
MAX_IMAGE_SIDE = 1600
JPEG_QUALITY = 85


def _load_display_image(photo: Photo) -> Optional[Image.Image]:
    """The EXIF-oriented display copy, downscaled to a print-friendly size."""
    path = LIBRARY_DIR / photo.display_filename
    if not path.is_file():
        return None
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im).convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    return img


def _image_reader(img: Image.Image) -> ImageReader:
    """Wrap the image as JPEG bytes so reportlab embeds it with DCT compression."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    buf.seek(0)
    return ImageReader(buf)


def _people_in_photo(session: Session, photo_id: int) -> List[str]:
    return list(
        session.exec(
            select(Person.name)
            .select_from(Face)
            .join(Person, Face.person_id == Person.id)
            .where(Face.photo_id == photo_id)
            .distinct()
            .order_by(Person.name)
        ).all()
    )


def _caption_lines(session: Session, photo: Photo) -> List[str]:
    """Wrapped caption text: the description followed by the tagged people."""
    max_w = PAGE_W - 2 * MARGIN
    lines: List[str] = []
    if photo.description:
        lines += simpleSplit(photo.description, BODY_FONT, BODY_SIZE, max_w)
    people = _people_in_photo(session, photo.id)
    if people:
        lines += simpleSplit(
            "People: " + ", ".join(people), BODY_FONT, BODY_SIZE, max_w
        )
    return lines


def _draw_cover(c: canvas.Canvas, album: Album, photo_count: int) -> None:
    c.setFont(BOLD_FONT, 28)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 2.5 * inch, album.name)
    c.setFont(BODY_FONT, 13)
    label = f"{photo_count} photo{'s' if photo_count != 1 else ''}"
    c.drawCentredString(PAGE_W / 2, PAGE_H - 2.5 * inch - 0.5 * inch, label)
    c.showPage()


def _draw_photo_page(
    c: canvas.Canvas, img: Image.Image, caption: List[str]
) -> None:
    caption_h = len(caption) * LINE_H + (0.3 * inch if caption else 0)
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = PAGE_H - 2 * MARGIN - caption_h

    iw, ih = img.size
    scale = min(avail_w / iw, avail_h / ih)
    draw_w, draw_h = iw * scale, ih * scale
    x = (PAGE_W - draw_w) / 2
    y = PAGE_H - MARGIN - draw_h
    c.drawImage(
        _image_reader(img), x, y, width=draw_w, height=draw_h, preserveAspectRatio=True
    )

    c.setFont(BODY_FONT, BODY_SIZE)
    ty = y - 0.3 * inch
    for line in caption:
        c.drawString(MARGIN, ty, line)
        ty -= LINE_H
    c.showPage()


def build_album_pdf(session: Session, album: Album) -> io.BytesIO:
    """Return an in-memory PDF of every photo in the album, newest imports last."""
    photos = session.exec(
        select(Photo)
        .where(Photo.album_id == album.id)
        .order_by(Photo.imported_at)
    ).all()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(album.name)

    _draw_cover(c, album, len(photos))
    for photo in photos:
        img = _load_display_image(photo)
        if img is None:
            continue
        _draw_photo_page(c, img, _caption_lines(session, photo))

    c.save()
    buffer.seek(0)
    return buffer
