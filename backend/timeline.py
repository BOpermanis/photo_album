"""Photo dating and the per-person chronological order.

Every undated photo gets ONE estimated year, shared by everyone in it, from a
single global optimisation over all facts and measurements:

Facts (fixed):
  * photo dates, and years pinned by dragging a photo on a person page;
  * birth / death years entered on a person (hard bounds on their photos).

Measurements (soft, each with its own uncertainty sigma, in years):
  * face similarity: for every person in an undated photo, the photo's year
    should match the similarity-weighted mean year of that person's most
    similar other photos (harmonic label propagation, Zhu et al. 2003). Sigma
    grows with how much the person's look-alike dated photos disagree, so an
    adult whose face barely changed for decades counts for little, while a
    child whose look-alikes cluster within a few years counts for a lot;
  * apparent age: photo year - birth year ~= the age model's estimate for a
    tagged face (sigma from the model's band probabilities, see age.py).
    Unknown birth years are unknowns of the same problem, calibrated by the
    person's dated photos.

Solved as robust weighted least squares (Huber loss via IRLS, so one badly
misjudged age or look-alike cannot drag a photo far), with birth/death bounds
enforced by a hinge active set. With only similarity terms and one person per
photo this reduces exactly to harmonic propagation.

Inputs change in many places (tags, dates, pins, ages computed by workers), so
writers call mark_dirty(); the API process re-solves in ensure_fresh() and
stores Photo.estimated_year, Person.estimated_birth_year and PersonPhotoOrder.
"""

import calendar
import threading
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Optional, Sequence

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve
from sqlalchemy import text
from sqlmodel import Session, delete, select

from .models import Face, Person, PersonPhotoOrder, Photo

# Softmax temperature on cosine similarity: ArcFace same-person similarities
# span roughly 0.3-0.9; at 0.05 a 0.1 similarity gap is a ~7x weight ratio.
TEMPERATURE = 0.05
K_NEIGHBOURS = 8
# Sigmas below were tuned by cross-validation (hiding dated photos and
# re-estimating them): look-alike estimates are more precise than their raw
# date spread suggests, while age readings are best taken at face value.
SIM_SIGMA_FLOOR = 1.0  # best-case precision of a look-alike estimate
SIM_SPREAD_SCALE = 0.5  # how much look-alikes' date disagreement widens sigma
SIM_SIGMA_DEFAULT = 12.0  # person has fewer than two dated photos
AGE_SIGMA_FLOOR = 1.5
AGE_SIGMA_SCALE = 1.0  # multiplier on the age model's own sd
HUBER = 1.0  # residuals beyond 1 sigma get linear (not quadratic) cost
PRIOR_SIGMA = 60.0  # weak pull to the collection's mean year (well-posedness)
BIRTH_PRIOR_OFFSET = 35.0
BOUND_SIGMA = 0.02  # stiffness of an active birth/death bound
BIRTH_MARGIN = 0.5  # birth years are mid-year; a photo may be from that year
DEATH_MARGIN = 1.0
MAX_ITERS = 30
MIN_YEAR = 1800.0
MAX_YEAR = float(_date.today().year + 1)


# ------------------------------ year helpers -------------------------------
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


# -------------------------------- the model --------------------------------
@dataclass
class Snapshot:
    """Everything the solver needs, detached from the database."""

    fixed: dict  # photo id -> fixed year, or None (to estimate)
    birth: dict  # person id -> known birth year (mid-year float)
    death: dict  # person id -> known death year
    person_photos: dict  # person id -> sorted photo ids they are tagged in
    embeddings: dict  # (person id, photo id) -> mean face embedding
    ages: list  # (person id, photo id, age, sd) per tagged face with an age


@dataclass
class Solution:
    photo_year: dict = field(default_factory=dict)  # estimated photos only
    birth_year: dict = field(default_factory=dict)  # estimated births only


def load(session: Session) -> Snapshot:
    embs: dict = {}
    ages = []
    person_photos: dict = {}
    for person_id, photo_id, emb, age, age_sd in session.exec(
        select(Face.person_id, Face.photo_id, Face.embedding, Face.age, Face.age_sd).where(
            Face.person_id.is_not(None)
        )
    ).all():
        person_photos.setdefault(person_id, set()).add(photo_id)
        if emb:
            embs.setdefault((person_id, photo_id), []).append(np.frombuffer(emb, np.float32))
        if age is not None:
            ages.append((person_id, photo_id, float(age), float(age_sd or AGE_SIGMA_FLOOR)))
    photo_ids = set().union(*person_photos.values()) if person_photos else set()
    fixed = {}
    for pid, date, pinned in session.exec(
        select(Photo.id, Photo.date, Photo.pinned_year)
    ).all():
        if pid in photo_ids:
            year = date_to_year(date)
            fixed[pid] = year if year is not None else pinned
    birth, death = {}, {}
    for pid, b, d in session.exec(
        select(Person.id, Person.birth_year, Person.death_year)
    ).all():
        if b is not None:
            birth[pid] = b + 0.5
        if d is not None:
            death[pid] = float(d)
    # Faces of deleted photos/persons can linger only transiently; drop them.
    person_photos = {
        i: sorted(p for p in ps if p in fixed) for i, ps in person_photos.items()
    }
    return Snapshot(
        fixed=fixed,
        birth=birth,
        death=death,
        person_photos=person_photos,
        embeddings={k: mean_embedding(v) for k, v in embs.items()},
        ages=[a for a in ages if a[1] in fixed],
    )


def solve(snap: Snapshot, use_age: bool = True, use_similarity: bool = True) -> Solution:
    fixed = snap.fixed
    var_photos = [p for p, y in fixed.items() if y is None]
    pidx = {p: k for k, p in enumerate(var_photos)}
    ages = snap.ages if use_age else []
    var_births = sorted({i for i, *_ in ages if i not in snap.birth})
    bidx = {i: len(var_photos) + k for k, i in enumerate(var_births)}
    n = len(var_photos) + len(var_births)
    if not var_photos:
        return Solution()
    known = [y for y in fixed.values() if y is not None]
    mu = float(np.mean(known)) if known else 1970.0

    # Measurement rows: sum(vals * x[cols]) ~= target, with std `sigma`.
    rows_c, rows_v, target, sigma = [], [], [], []
    sim_informed: set = set()

    def add(cols, vals, tgt, sd):
        rows_c.append(cols)
        rows_v.append(vals)
        target.append(tgt)
        sigma.append(sd)

    if use_similarity:
        for i, photos in snap.person_photos.items():
            ids = [p for p in photos if snap.embeddings.get((i, p)) is not None]
            if len(ids) < 2 or all(fixed[p] is not None for p in ids):
                continue
            E = np.stack([snap.embeddings[(i, p)] for p in ids]).astype(np.float64)
            W = np.exp((E @ E.T - 1.0) / TEMPERATURE)
            np.fill_diagonal(W, 0.0)
            is_fixed = np.array([fixed[p] is not None for p in ids])
            fy = np.array([fixed[p] if fixed[p] is not None else 0.0 for p in ids])
            for a, p in enumerate(ids):
                if is_fixed[a]:
                    continue
                w = W[a]
                nb = np.argsort(-w)[:K_NEIGHBOURS]
                nb = nb[w[nb] > 0]
                if not len(nb):
                    continue
                wn = w[nb] / w[nb].sum()
                cols, vals, tgt = [pidx[p]], [1.0], 0.0
                for q, wq in zip(nb, wn):
                    if is_fixed[q]:
                        tgt += wq * fy[q]
                    else:
                        cols.append(pidx[ids[q]])
                        vals.append(-wq)
                wf = w * is_fixed
                if is_fixed.sum() >= 2 and wf.sum() > 0:
                    wf = wf / wf.sum()
                    m = wf @ fy
                    spread = float(np.sqrt(wf @ (fy - m) ** 2))
                    sd = float(np.hypot(SIM_SIGMA_FLOOR, SIM_SPREAD_SCALE * spread))
                else:
                    sd = SIM_SIGMA_DEFAULT
                add(cols, vals, tgt, sd)
                sim_informed.add(p)

    # Apparent age: photo year - birth year = age.
    for i, p, age, age_sd in ages:
        cols, vals, tgt = [], [], age
        if fixed[p] is None:
            cols.append(pidx[p])
            vals.append(1.0)
        else:
            tgt -= fixed[p]
        if i in snap.birth:
            tgt += snap.birth[i]
        else:
            cols.append(bidx[i])
            vals.append(-1.0)
        if cols:
            add(cols, vals, tgt, max(age_sd * AGE_SIGMA_SCALE, AGE_SIGMA_FLOOR))

    # Which unknowns are actually determined by evidence (not just the prior).
    anchored = set(snap.birth) | {
        i for i, p, *_ in ages if fixed[p] is not None or p in sim_informed
    }
    informed = set(sim_informed) | {
        p for i, p, *_ in ages if fixed[p] is None and i in anchored
    }

    # Birth/death bounds: sum(vals * x[cols]) >= rhs.
    bounds = []
    for i, photos in snap.person_photos.items():
        b_fixed = snap.birth.get(i)
        for p in photos:
            t_var = fixed[p] is None
            # t - b >= -BIRTH_MARGIN
            cols, vals, rhs = [], [], -BIRTH_MARGIN
            if t_var:
                cols.append(pidx[p])
                vals.append(1.0)
            else:
                rhs -= fixed[p]
            if b_fixed is not None:
                rhs += b_fixed
            elif i in bidx:
                cols.append(bidx[i])
                vals.append(-1.0)
            else:
                cols = []
            if cols and (t_var or i in bidx):
                bounds.append((cols, vals, rhs))
            # -t >= -(death + margin)
            if t_var and i in snap.death:
                bounds.append(([pidx[p]], [-1.0], -(snap.death[i] + DEATH_MARGIN)))

    m_rows = len(target)
    A = _sparse(rows_c, rows_v, m_rows, n)
    target = np.array(target)
    sigma = np.array(sigma)
    prior_mean = np.full(n, mu)
    prior_mean[len(var_photos):] = mu - BIRTH_PRIOR_OFFSET
    prior_w = np.full(n, 1.0 / PRIOR_SIGMA**2)
    if bounds:
        B = _sparse([b[0] for b in bounds], [b[1] for b in bounds], len(bounds), n)
        b_rhs = np.array([b[2] for b in bounds])
    x = prior_mean.copy()
    active = np.zeros(len(bounds), dtype=bool)
    for it in range(MAX_ITERS):
        if it == 0 or not m_rows:
            robust = np.ones(m_rows)
        else:
            z = np.abs(A @ x - target) / sigma
            robust = np.where(z > HUBER, HUBER / np.maximum(z, 1e-12), 1.0)
        omega = robust / sigma**2
        N = (A.T @ sp.diags(omega) @ A) + sp.diags(prior_w)
        rhs = A.T @ (omega * target) + prior_w * prior_mean
        if active.any():
            Ba = B[active]
            N = N + (Ba.T @ Ba) / BOUND_SIGMA**2
            rhs = rhs + Ba.T @ b_rhs[active] / BOUND_SIGMA**2
        x_new = np.atleast_1d(spsolve(sp.csc_matrix(N), rhs))
        # Semismooth Newton on the hinge: a bound is active while violated.
        new_active = (B @ x_new - b_rhs < -1e-6) if bounds else active
        converged = (
            np.array_equal(new_active, active) and np.max(np.abs(x_new - x)) < 1e-4
        )
        x, active = x_new, new_active
        if converged:
            break

    sol = Solution()
    for p in var_photos:
        if p in informed:
            sol.photo_year[p] = float(np.clip(x[pidx[p]], MIN_YEAR, MAX_YEAR))
    for i in var_births:
        if i in anchored:
            sol.birth_year[i] = float(x[bidx[i]])
    return sol


def _sparse(cols_list, vals_list, m, n):
    r = np.repeat(np.arange(m), [len(c) for c in cols_list]) if m else np.zeros(0, int)
    c = np.concatenate([np.asarray(c, int) for c in cols_list]) if m else np.zeros(0, int)
    v = np.concatenate([np.asarray(v, float) for v in vals_list]) if m else np.zeros(0)
    return sp.csr_matrix((v, (r, c)), shape=(m, n))


def person_order(snap: Snapshot, sol: Solution, person_id: int) -> list[int]:
    """Oldest first; on equal years fixed photos before estimated ones; photos
    that could not be placed go last."""

    def key(p):
        y = snap.fixed[p] if snap.fixed[p] is not None else sol.photo_year.get(p)
        return (y is None, y if y is not None else 0.0, snap.fixed[p] is None, p)

    return sorted(snap.person_photos.get(person_id, []), key=key)


# ------------------------------- persistence -------------------------------
_solve_lock = threading.Lock()


def mark_dirty(session: Session) -> None:
    """Record that timeline inputs changed (safe from any process)."""
    session.execute(
        text("UPDATE timelinestate SET change_version = change_version + 1 WHERE id = 1")
    )
    session.commit()


def _versions(session: Session):
    return session.execute(
        text("SELECT change_version, computed_version FROM timelinestate WHERE id = 1")
    ).one_or_none()


def ensure_fresh(session: Session) -> bool:
    """Re-solve and store if inputs changed since the last solve."""
    session.commit()  # end any open read transaction to see the latest state
    v = _versions(session)
    if v is None or v[0] <= v[1]:
        return False
    with _solve_lock:
        session.commit()
        v = _versions(session)
        if v[0] <= v[1]:
            return False
        snap = load(session)
        sol = solve(snap)
        _store(session, snap, sol, v[0])
    return True


def refresh(session: Session) -> None:
    mark_dirty(session)
    ensure_fresh(session)


def _store(session: Session, snap: Snapshot, sol: Solution, version: int) -> None:
    session.execute(text("UPDATE photo SET estimated_year = NULL WHERE estimated_year IS NOT NULL"))
    if sol.photo_year:
        session.execute(
            text("UPDATE photo SET estimated_year = :y WHERE id = :id"),
            [{"id": p, "y": y} for p, y in sol.photo_year.items()],
        )
    session.execute(
        text("UPDATE person SET estimated_birth_year = NULL WHERE estimated_birth_year IS NOT NULL")
    )
    if sol.birth_year:
        session.execute(
            text("UPDATE person SET estimated_birth_year = :y WHERE id = :id"),
            [{"id": i, "y": y} for i, y in sol.birth_year.items()],
        )
    session.exec(delete(PersonPhotoOrder))
    session.add_all(
        PersonPhotoOrder(
            person_id=i,
            photo_id=p,
            position=pos,
            estimated_year=sol.photo_year.get(p),
        )
        for i in snap.person_photos
        for pos, p in enumerate(person_order(snap, sol, i))
    )
    session.execute(
        text("UPDATE timelinestate SET computed_version = :v WHERE id = 1 AND computed_version < :v"),
        {"v": version},
    )
    session.commit()


def delete_photo_rows(session: Session, photo_id: int) -> None:
    """Drop a photo from every person's stored order (caller commits)."""
    session.exec(delete(PersonPhotoOrder).where(PersonPhotoOrder.photo_id == photo_id))


def ordered_photos(session: Session, person_id: int) -> list[PersonPhotoOrder]:
    """The stored order for a person, re-solved first when inputs changed or
    the stored set no longer matches the photos the person is tagged in."""
    ensure_fresh(session)

    def load_rows():
        return session.exec(
            select(PersonPhotoOrder)
            .where(PersonPhotoOrder.person_id == person_id)
            .order_by(PersonPhotoOrder.position)
        ).all()

    rows = load_rows()
    tagged = set(
        session.exec(
            select(Face.photo_id).where(Face.person_id == person_id).distinct()
        ).all()
    )
    if {r.photo_id for r in rows} != tagged:
        refresh(session)
        rows = load_rows()
    return rows
