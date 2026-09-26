import time

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from .config import ANNOY_PATH, DB_PATH

# Bump when a migration needs a one-time data change (not just new columns).
SCHEMA_VERSION = 2


def _make_engine():
    """Build an engine with SQLite pragmas suited to multi-process access.

    WAL lets the API process and the worker pool read/write concurrently;
    busy_timeout makes writers wait for the lock instead of erroring.
    """
    eng = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return eng


# Module-level engine for the API process. Worker processes build their own via
# make_engine() so SQLite connections are never shared across a process boundary.
engine = _make_engine()


def make_engine():
    return _make_engine()


def init_db() -> None:
    from . import models  # noqa: F401  ensure tables are registered

    SQLModel.metadata.create_all(engine)
    _run_migrations()


def _run_migrations() -> None:
    """Add columns introduced after the DB was first created and run one-time
    data migrations keyed off PRAGMA user_version."""
    with engine.begin() as conn:
        photo_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(photo)"))}
        if "description" not in photo_cols:
            conn.execute(text("ALTER TABLE photo ADD COLUMN description TEXT DEFAULT ''"))
        if "edited_filename" not in photo_cols:
            conn.execute(
                text("ALTER TABLE photo ADD COLUMN edited_filename TEXT DEFAULT ''")
            )
        if "edit_params" not in photo_cols:
            conn.execute(
                text("ALTER TABLE photo ADD COLUMN edit_params TEXT DEFAULT ''")
            )
        if "album_id" not in photo_cols:
            conn.execute(text("ALTER TABLE photo ADD COLUMN album_id INTEGER"))
        if "original_width" not in photo_cols:
            conn.execute(
                text("ALTER TABLE photo ADD COLUMN original_width INTEGER DEFAULT 0")
            )
        if "original_height" not in photo_cols:
            conn.execute(
                text("ALTER TABLE photo ADD COLUMN original_height INTEGER DEFAULT 0")
            )

        face_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(face)"))}
        if "poly_original" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN poly_original TEXT DEFAULT ''"))
        if "poly_aligned" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN poly_aligned TEXT DEFAULT ''"))
        if "embedding" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN embedding BLOB"))

        # Ensure at least one album exists and every photo belongs to one.
        default_album_id = conn.execute(
            text("SELECT id FROM album ORDER BY id LIMIT 1")
        ).scalar()
        if default_album_id is None:
            conn.execute(
                text("INSERT INTO album (name, created_at) VALUES ('Album_1', :ts)"),
                {"ts": _now_iso()},
            )
            default_album_id = conn.execute(
                text("SELECT id FROM album ORDER BY id LIMIT 1")
            ).scalar()
        conn.execute(
            text("UPDATE photo SET album_id = :aid WHERE album_id IS NULL"),
            {"aid": default_album_id},
        )

        version = conn.execute(text("PRAGMA user_version")).scalar() or 0
        if version < SCHEMA_VERSION:
            _wipe_and_requeue(conn)
            conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))


def _wipe_and_requeue(conn) -> None:
    """One-time reset: drop old rectangle-only detections and re-detect them as
    polygons. Existing name assignments and the recognition index are dropped."""
    conn.execute(text("DELETE FROM face"))
    conn.execute(text("DELETE FROM faceindexentry"))
    conn.execute(text("UPDATE photo SET processed = 0"))
    now = time.time()
    photos = list(conn.execute(text("SELECT id, COALESCE(edit_params, '') FROM photo")))
    for pid, edit_params in photos:
        conn.execute(
            text(
                "INSERT INTO job (photo_id, kind, status, priority, payload, error, "
                "created_at, updated_at) VALUES (:pid, 'detect', 'pending', 0, '', '', "
                ":now, :now)"
            ),
            {"pid": pid, "now": now},
        )
        if edit_params:
            conn.execute(
                text(
                    "INSERT INTO job (photo_id, kind, status, priority, payload, error, "
                    "created_at, updated_at) VALUES (:pid, 'align', 'pending', 10, '', "
                    "'', :now, :now)"
                ),
                {"pid": pid, "now": now},
            )
    try:
        ANNOY_PATH.unlink(missing_ok=True)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def get_session():
    with Session(engine) as session:
        yield session
