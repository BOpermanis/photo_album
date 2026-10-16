"""Chronological ordering of one person's photos, with date estimates for the
undated ones.

Dated photos anchor the timeline, as do undated photos the user dragged into
place (Photo.pinned_year). Every other undated photo gets a year estimated
from how similar the person's face in it is to their face in all the other
photos, using harmonic-function label propagation (Zhu, Ghahramani & Lafferty,
2003): on a fully connected similarity graph, each undated photo's year is the
similarity-weighted average of its neighbours' years, solved jointly so chains
of look-alike undated photos are placed together. A face ages gradually, so
similar faces tend to be close in time.

Estimates are convex combinations of known dates, so they always fall between
the earliest and latest dated photo (interpolation, never extrapolation).

The result is stored in PersonPhotoOrder and recomputed via reorder_persons()
whenever a person gains or loses a photo or a photo's date changes.
"""

import calendar
from datetime import date as _date
from typing import Iterable, Optional, Sequence

import numpy as np
from sqlmodel import Session, delete, select

from .models import Face, Person, PersonPhotoOrder, Photo

# Softmax-like temperature on cosine similarity. ArcFace same-person similarities
# span roughly 0.3-0.9; at 0.05 a 0.1 similarity gap is a ~7x weight ratio, so
# the closest faces dominate without hard k-NN cut-offs (which could disconnect
# the graph and make the system singular).
TEMPERATURE = 0.05


def date_to_year(value: str) -> Optional[float]:
    """"YYYY" / "YYYY/MM" / "YYYY/MM/DD" -> fractional year at the middle of the
    stated period; "" -> None."""
    if not value:
        return None
    try:
        parts = [int(p) for p in value.split("/")]
    except ValueError:
        return None
    year = parts[0]
    if len(parts) == 1:
        return year + 0.5
    month = parts[1]
    if len(parts) == 2:
        return year + (month - 0.5) / 12
    days = 366 if calendar.isleap(year) else 365
    doy = _date(year, month, parts[2]).timetuple().tm_yday
    return year + (doy - 0.5) / days


def anchor_year(photo: Photo) -> Optional[float]:
    """The photo's fixed year: its real date, else a manually pinned year."""
    year = date_to_year(photo.date)
    return year if year is not None else photo.pinned_year


def pin_between(
    prev_year: Optional[float], next_year: Optional[float]
) -> Optional[float]:
    """Year for a photo dropped between two neighbours (either may be missing
    at the ends of the timeline): the midpoint, or half a year beyond the one
    neighbour."""
    if prev_year is not None and next_year is not None:
        return (prev_year + next_year) / 2
    if prev_year is not None:
        return prev_year + 0.5
    if next_year is not None:
        return next_year - 0.5
    return None


def mean_embedding(embeddings: Sequence[np.ndarray]) -> Optional[np.ndarray]:
    """L2-normalised mean of a photo's faces of one person (usually just one)."""
    if not embeddings:
        return None
    vs = np.stack([e / (np.linalg.norm(e) or 1.0) for e in embeddings])
    m = vs.mean(axis=0)
    n = np.linalg.norm(m)
    return m / n if n > 0 else None


def estimate_years(
    known: Sequence[Optional[float]], embeddings: Sequence[Optional[np.ndarray]]
) -> list[Optional[float]]:
    """Fill in the missing years of `known` from embedding similarity.

    Known years are returned unchanged. Unknown ones stay None when there is no
    embedding for them or no dated photo with an embedding to anchor on."""
    out = list(known)
    idx = [i for i, e in enumerate(embeddings) if e is not None]
    dated = [i for i in idx if known[i] is not None]
    undated = [i for i in idx if known[i] is None]
    if not dated or not undated:
        return out

    E = np.stack([embeddings[i] for i in dated + undated]).astype(np.float64)
    S = E @ E.T
    W = np.exp((S - 1.0) / TEMPERATURE)
    np.fill_diagonal(W, 0.0)

    nd = len(dated)
    W_ud = W[nd:, :nd]
    W_uu = W[nd:, nd:]
    f_d = np.array([known[i] for i in dated], dtype=np.float64)
    # Harmonic solution: (D_uu - W_uu) f_u = W_ud f_d. The matrix is strictly
    # diagonally dominant (every W_ud row sum > 0), hence always invertible.
    L_uu = np.diag(W[nd:].sum(axis=1)) - W_uu
    f_u = np.linalg.solve(L_uu, W_ud @ f_d)

    lo, hi = f_d.min(), f_d.max()
    for i, y in zip(undated, f_u):
        out[i] = float(np.clip(y, lo, hi))
    return out


# ------------------------------ persistence --------------------------------
def _person_photo_ids(session: Session, person_id: int) -> set[int]:
    return set(
        session.exec(
            select(Face.photo_id).where(Face.person_id == person_id).distinct()
        ).all()
    )


def _compute_order(session: Session, person_id: int) -> list[PersonPhotoOrder]:
    embs_by_photo: dict[int, list[np.ndarray]] = {}
    for photo_id, emb in session.exec(
        select(Face.photo_id, Face.embedding).where(Face.person_id == person_id)
    ).all():
        embs = embs_by_photo.setdefault(photo_id, [])
        if emb:
            embs.append(np.frombuffer(emb, np.float32))
    if not embs_by_photo:
        return []
    photos = session.exec(select(Photo).where(Photo.id.in_(list(embs_by_photo)))).all()

    known = [anchor_year(p) for p in photos]
    years = estimate_years(known, [mean_embedding(embs_by_photo[p.id]) for p in photos])
    # Oldest first; dated photos before estimated ones on ties; photos that
    # could not be placed go last.
    order = sorted(
        range(len(photos)),
        key=lambda i: (
            years[i] is None,
            years[i] if years[i] is not None else 0.0,
            known[i] is None,
            photos[i].id,
        ),
    )
    return [
        PersonPhotoOrder(
            person_id=person_id,
            photo_id=photos[i].id,
            position=pos,
            estimated_year=years[i] if known[i] is None else None,
        )
        for pos, i in enumerate(order)
    ]


def reorder_persons(session: Session, person_ids: Iterable[Optional[int]]) -> None:
    """Recompute and store the chronological photo order of each person, then
    commit. Call after a person gains/loses a photo or a photo's date changes."""
    ids = {pid for pid in person_ids if pid is not None}
    if not ids:
        return
    for pid in ids:
        rows = _compute_order(session, pid)
        session.exec(delete(PersonPhotoOrder).where(PersonPhotoOrder.person_id == pid))
        session.add_all(rows)
    session.commit()


def reorder_all(session: Session) -> None:
    reorder_persons(session, session.exec(select(Person.id)).all())


def persons_in_photos(session: Session, photo_ids: Iterable[int]) -> set[int]:
    ids = list(photo_ids)
    if not ids:
        return set()
    rows = session.exec(
        select(Face.person_id).where(Face.photo_id.in_(ids)).distinct()
    ).all()
    return {pid for pid in rows if pid is not None}


def delete_photo_rows(session: Session, photo_id: int) -> None:
    """Drop a photo from every person's stored order (caller commits)."""
    session.exec(delete(PersonPhotoOrder).where(PersonPhotoOrder.photo_id == photo_id))


def ordered_photos(session: Session, person_id: int) -> list[PersonPhotoOrder]:
    """The stored order for a person. Recomputed first if it no longer matches
    the photos the person is tagged in (e.g. after a migration-time person
    merge), so readers never see a stale photo set."""

    def load():
        return session.exec(
            select(PersonPhotoOrder)
            .where(PersonPhotoOrder.person_id == person_id)
            .order_by(PersonPhotoOrder.position)
        ).all()

    rows = load()
    if {r.photo_id for r in rows} != _person_photo_ids(session, person_id):
        reorder_persons(session, [person_id])
        rows = load()
    return rows
