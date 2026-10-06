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
        if "taken_at" not in photo_cols:
            conn.execute(text("ALTER TABLE photo ADD COLUMN taken_at TIMESTAMP"))
        _backfill_taken_at(conn)

        face_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(face)"))}
        if "poly_original" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN poly_original TEXT DEFAULT ''"))
        if "poly_aligned" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN poly_aligned TEXT DEFAULT ''"))
        if "embedding" not in face_cols:
            conn.execute(text("ALTER TABLE face ADD COLUMN embedding BLOB"))

        rel_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(personrelation)"))
        }
        if "is_auto" not in rel_cols:
            conn.execute(
                text(
                    "ALTER TABLE personrelation ADD COLUMN is_auto BOOLEAN "
                    "DEFAULT 0"
                )
            )

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

        _migrate_persons(conn)

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


# People confirmed by the user to be the same individual (matched on the
# original free-text name). The row with the most structured name parts
# survives; the rest are repointed onto it and deleted.
_CONFIRMED_MERGES = [
    ["Aija", "Aija Zariņa"],
    ["Anna Gaigula", "Anna Gaigula (Tatas māte)"],
    ['Astrīde "Melnā"', 'Astrīde "Melnā" Matīsa'],
    ["Auseklis", "Auseklis Šveicers"],
    ["Biruta Krišjāne", "Biruta Krišjāne (Ulpe)", "Biruta Ulpe"],
    ["Maija", "Maija Opermane"],
    ["Rudīte Auziņa", "Rudīte Auziņa (kādreiz Luca)", "Rudīte Luca"],
    ["Velta", "Velta Vīksnes"],
]

_KIND_PRIORITY = {"maiden": 3, "nickname": 2, "surname": 1}


def _migrate_persons(conn) -> None:
    """One-time Phase-1 migration: rebuild the person table with structured
    columns, parse each legacy free-text name into names/professions/relations,
    then apply the user-confirmed duplicate merges. Guarded so it runs once."""
    from . import person_parse

    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(person)"))}
    if not cols or "given_name" in cols:
        return  # fresh DB (already new schema) or already migrated

    rows = list(conn.execute(text("SELECT id, name, created_at FROM person")))

    conn.execute(text("ALTER TABLE person RENAME TO person_old"))
    conn.execute(
        text(
            "CREATE TABLE person (id INTEGER PRIMARY KEY, given_name TEXT DEFAULT '', "
            "display_name TEXT DEFAULT '', raw_name TEXT DEFAULT '', notes TEXT "
            "DEFAULT '', created_at TIMESTAMP)"
        )
    )

    for pid, name, created_at in rows:
        parsed = person_parse.parse_name(name)
        display = person_parse.compute_display(parsed["given"], parsed["surnames"], name)
        conn.execute(
            text(
                "INSERT INTO person (id, given_name, display_name, raw_name, notes, "
                "created_at) VALUES (:id, :g, :d, :r, '', :c)"
            ),
            {"id": pid, "g": parsed["given"], "d": display, "r": name, "c": created_at},
        )
        for i, (value, kind) in enumerate(parsed["surnames"]):
            conn.execute(
                text(
                    "INSERT INTO personname (person_id, value, kind, is_primary, sort) "
                    "VALUES (:p, :v, :k, :ip, :s)"
                ),
                {"p": pid, "v": value, "k": kind, "ip": 1 if i == 0 else 0, "s": i},
            )
        for title, place, start, end, note in parsed["professions"]:
            conn.execute(
                text(
                    "INSERT INTO personprofession (person_id, title, place, "
                    "start_year, end_year, note) VALUES (:p, :t, :pl, :sy, :ey, :n)"
                ),
                {"p": pid, "t": title, "pl": place, "sy": start, "ey": end, "n": note},
            )
        for kind, target_raw, guess, note in parsed["relations"]:
            conn.execute(
                text(
                    "INSERT INTO personrelation (person_id, related_person_id, "
                    "related_name_raw, kind, custom_label, note) VALUES "
                    "(:p, NULL, :raw, :k, '', :n)"
                ),
                {"p": pid, "raw": guess or target_raw, "k": kind, "n": note},
            )

    conn.execute(text("DROP TABLE person_old"))
    _apply_confirmed_merges(conn)


def _apply_confirmed_merges(conn) -> None:
    from . import person_parse

    for group in _CONFIRMED_MERGES:
        members = []
        for raw in group:
            row = conn.execute(
                text("SELECT id FROM person WHERE raw_name = :r"), {"r": raw}
            ).first()
            if row:
                members.append(row[0])
        if len(members) < 2:
            continue

        def _score(pid):
            n = conn.execute(
                text("SELECT COUNT(*) FROM personname WHERE person_id = :p"), {"p": pid}
            ).scalar()
            has_given = conn.execute(
                text("SELECT given_name FROM person WHERE id = :p"), {"p": pid}
            ).scalar()
            return (n, 1 if has_given else 0, -pid)

        canonical = max(members, key=_score)
        for mid in members:
            if mid == canonical:
                continue
            for tbl in ("face", "faceindexentry", "personname",
                        "personprofession", "personrelation"):
                conn.execute(
                    text(f"UPDATE {tbl} SET person_id = :c WHERE person_id = :m"),
                    {"c": canonical, "m": mid},
                )
            conn.execute(
                text(
                    "UPDATE personrelation SET related_person_id = :c "
                    "WHERE related_person_id = :m"
                ),
                {"c": canonical, "m": mid},
            )
            conn.execute(text("DELETE FROM person WHERE id = :m"), {"m": mid})

        _dedup_names_and_display(conn, canonical, person_parse)


def _dedup_names_and_display(conn, pid, person_parse) -> None:
    """Collapse duplicate name values (keeping the strongest kind) and refresh
    the canonical person's display_name from the merged parts."""
    rows = list(
        conn.execute(
            text("SELECT value, kind FROM personname WHERE person_id = :p"), {"p": pid}
        )
    )
    best = {}
    for value, kind in rows:
        key = person_parse.strip_accents(value).lower()
        if key not in best or _KIND_PRIORITY[kind] > _KIND_PRIORITY[best[key][1]]:
            best[key] = (value, kind)

    conn.execute(text("DELETE FROM personname WHERE person_id = :p"), {"p": pid})
    names = list(best.values())
    for i, (value, kind) in enumerate(names):
        conn.execute(
            text(
                "INSERT INTO personname (person_id, value, kind, is_primary, sort) "
                "VALUES (:p, :v, :k, :ip, :s)"
            ),
            {"p": pid, "v": value, "k": kind, "ip": 1 if i == 0 else 0, "s": i},
        )

    given = conn.execute(
        text("SELECT given_name FROM person WHERE id = :p"), {"p": pid}
    ).scalar() or ""
    raw = conn.execute(
        text("SELECT raw_name FROM person WHERE id = :p"), {"p": pid}
    ).scalar() or ""
    display = person_parse.compute_display(given, names, raw)
    conn.execute(
        text("UPDATE person SET display_name = :d WHERE id = :p"),
        {"d": display, "p": pid},
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _backfill_taken_at(conn) -> None:
    """Fill taken_at for rows that lack it: EXIF capture time, else the import
    time. Runs once per photo since it only touches NULL rows."""
    from .config import LIBRARY_DIR
    from .upload import extract_taken_at

    rows = list(
        conn.execute(
            text("SELECT id, filename, imported_at FROM photo WHERE taken_at IS NULL")
        )
    )
    for pid, filename, imported_at in rows:
        taken = extract_taken_at(LIBRARY_DIR / filename)
        value = taken.isoformat() if taken else imported_at
        conn.execute(
            text("UPDATE photo SET taken_at = :ts WHERE id = :pid"),
            {"ts": value, "pid": pid},
        )


def get_session():
    with Session(engine) as session:
        yield session
