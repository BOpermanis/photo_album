"""Persistent job queue backing the multiprocessing worker pool.

Ordering is LIFO within a priority band: interactive work (align, rerun) has a
higher priority than bulk detection, and newest-first (`id DESC`) within each
band, so recent alignment fixes run while slow detections wait in the back.
"""

import time

from sqlalchemy import text
from sqlmodel import Session, select

from .models import Job

PRIORITY = {"detect": 0, "align": 10, "rerun_detect": 10}
ACTIVE = ("pending", "running")


def enqueue(session: Session, photo_id: int, kind: str, payload: str = "") -> Job:
    job = Job(
        photo_id=photo_id,
        kind=kind,
        priority=PRIORITY.get(kind, 0),
        payload=payload,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def claim_next(session: Session, worker_pid: int, kinds=None):
    """Atomically claim the next pending job for this process.

    A conditional UPDATE guarded by `status='pending'` guarantees that two
    worker processes can never grab the same row, even under WAL concurrency.
    Pass `kinds` to restrict a worker to a subset of job kinds (e.g. an
    align-only interactive lane). Returns a detached tuple
    (id, photo_id, kind, payload) or None.
    """
    now = time.time()
    params = {"pid": worker_pid, "now": now}
    kind_clause = ""
    if kinds:
        names = {f"k{i}": k for i, k in enumerate(kinds)}
        params.update(names)
        kind_clause = " AND kind IN (" + ",".join(f":{n}" for n in names) + ")"
    res = session.execute(
        text(
            "UPDATE job SET status='running', worker_pid=:pid, started_at=:now, "
            "updated_at=:now WHERE id = ("
            "  SELECT id FROM job WHERE status='pending'" + kind_clause + " "
            "  ORDER BY priority DESC, id DESC LIMIT 1"
            ") AND status='pending'"
        ),
        params,
    )
    session.commit()
    if not res.rowcount:
        return None
    # Exactly one running job belongs to this pid (workers run one at a time).
    job = session.exec(
        select(Job)
        .where(Job.worker_pid == worker_pid, Job.status == "running")
        .order_by(Job.id.desc())
    ).first()
    if job is None:
        return None
    return (job.id, job.photo_id, job.kind, job.payload)


def mark(session: Session, job_id: int, status: str, error: str = "") -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    job.status = status
    job.error = error
    job.updated_at = time.time()
    session.add(job)
    session.commit()


def requeue_stale(session: Session) -> int:
    """Reset jobs stuck in `running` (e.g. from a crashed process) to pending.

    Safe to call only at startup, when no workers are alive yet.
    """
    stale = session.exec(select(Job).where(Job.status == "running")).all()
    for job in stale:
        job.status = "pending"
        job.worker_pid = None
        job.updated_at = time.time()
        session.add(job)
    session.commit()
    return len(stale)


def pending_flags(session: Session, photo_id: int) -> dict:
    kinds = set(
        session.exec(
            select(Job.kind).where(
                Job.photo_id == photo_id, Job.status.in_(ACTIVE)
            )
        ).all()
    )
    return {
        "detect_pending": bool({"detect", "rerun_detect"} & kinds),
        "align_pending": "align" in kinds,
    }
