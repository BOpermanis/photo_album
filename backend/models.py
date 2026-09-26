from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Person(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=_utcnow)


class Photo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    content_hash: str = Field(index=True, unique=True)
    width: int = 0
    height: int = 0
    imported_at: datetime = Field(default_factory=_utcnow)
    processed: bool = Field(default=False)
    description: str = Field(default="")
    # Perspective-corrected copy of the original (empty = none). The original
    # upload is always kept; faces and display use the edited copy when present.
    edited_filename: str = Field(default="")
    # JSON of the last warp: {"rotation": int, "points": [[fx, fy], ...]}.
    edit_params: str = Field(default="")

    @property
    def display_filename(self) -> str:
        return self.edited_filename or self.filename


class Face(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    photo_id: int = Field(foreign_key="photo.id", index=True)
    # Bounding box in pixels of the EXIF-oriented image.
    x: float
    y: float
    w: float
    h: float
    confidence: float = 0.0
    person_id: Optional[int] = Field(default=None, foreign_key="person.id", index=True)


class FaceIndexEntry(SQLModel, table=True):
    # Maps an Annoy item slot to the reference face/person it was built from.
    item: int = Field(primary_key=True)
    face_id: int = Field(foreign_key="face.id", index=True)
    person_id: int = Field(foreign_key="person.id", index=True)
