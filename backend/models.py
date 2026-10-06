import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import LargeBinary
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Album(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=_utcnow)


class Person(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # First name(s). May be empty for occupation/relation-only placeholders.
    given_name: str = Field(default="")
    # Denormalized label recomputed from the structured fields on every write;
    # this is what the UI, recognition suggestions, and PDF export render.
    display_name: str = Field(default="", index=True)
    # The original free-text name this person was migrated from (provenance).
    raw_name: str = Field(default="")
    notes: str = Field(default="")
    created_at: datetime = Field(default_factory=_utcnow)


class PersonName(SQLModel, table=True):
    # A surname/nickname/maiden name; a person may have several.
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(foreign_key="person.id", index=True)
    value: str
    kind: str = Field(default="surname")  # surname | nickname | maiden
    is_primary: bool = Field(default=False)
    sort: int = Field(default=0)


class PersonProfession(SQLModel, table=True):
    # A profession held over an optional (year-granularity) time interval.
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(foreign_key="person.id", index=True)
    title: str
    place: str = Field(default="")
    start_year: Optional[int] = Field(default=None)
    end_year: Optional[int] = Field(default=None)
    note: str = Field(default="")


class PersonRelation(SQLModel, table=True):
    # Directed: "person_id is <kind> of related_person_id". The inverse is
    # derived for display only. related_name_raw holds an unresolved target.
    id: Optional[int] = Field(default=None, primary_key=True)
    person_id: int = Field(foreign_key="person.id", index=True)
    related_person_id: Optional[int] = Field(
        default=None, foreign_key="person.id", index=True
    )
    related_name_raw: str = Field(default="")
    # A preset key (son, daughter, father, mother, husband, wife, brother,
    # sister, grandfather, grandmother, friend) or the sentinel "custom".
    kind: str = Field(default="custom")
    custom_label: str = Field(default="")
    note: str = Field(default="")


class Photo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    album_id: Optional[int] = Field(default=None, foreign_key="album.id", index=True)
    filename: str
    content_hash: str = Field(index=True, unique=True)
    # Display dimensions: the aligned copy's size when present, else the original.
    width: int = 0
    height: int = 0
    # Dimensions of the EXIF-oriented original (poly_original lives in this space).
    original_width: int = 0
    original_height: int = 0
    imported_at: datetime = Field(default_factory=_utcnow)
    # EXIF capture time (DateTimeOriginal) when available, else the import time.
    # The Photos grid orders by this, oldest first.
    taken_at: Optional[datetime] = Field(default=None)
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
    # Axis-aligned bounding box in pixels of the EXIF-oriented original.
    x: float
    y: float
    w: float
    h: float
    # JSON list of 4 [x, y] points; poly_original in original space, poly_aligned
    # in aligned space (empty when there is no alignment). A homography maps one
    # to the other so warped (non-rectangular) boxes stay a bijection.
    poly_original: str = Field(default="")
    poly_aligned: str = Field(default="")
    # 512-float32 recognition embedding computed at detection time (nullable).
    embedding: Optional[bytes] = Field(default=None, sa_type=LargeBinary)
    confidence: float = 0.0
    person_id: Optional[int] = Field(default=None, foreign_key="person.id", index=True)


class Job(SQLModel, table=True):
    # Persistent background-work queue processed by the multiprocessing pool.
    id: Optional[int] = Field(default=None, primary_key=True)
    photo_id: int = Field(foreign_key="photo.id", index=True)
    kind: str = Field(index=True)  # "detect" | "align" | "rerun_detect"
    status: str = Field(default="pending", index=True)  # pending|running|done|failed
    priority: int = Field(default=0, index=True)  # higher runs first
    payload: str = Field(default="")
    error: str = Field(default="")
    worker_pid: Optional[int] = Field(default=None)
    created_at: float = Field(default_factory=time.time)
    started_at: Optional[float] = Field(default=None)
    updated_at: float = Field(default_factory=time.time)


class FaceIndexEntry(SQLModel, table=True):
    # Maps an Annoy item slot to the reference face/person it was built from.
    item: int = Field(primary_key=True)
    face_id: int = Field(foreign_key="face.id", index=True)
    person_id: int = Field(foreign_key="person.id", index=True)
