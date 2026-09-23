from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from .config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    from . import models  # noqa: F401  ensure tables are registered

    SQLModel.metadata.create_all(engine)
    _run_migrations()


def _run_migrations() -> None:
    """Add columns introduced after the DB was first created."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(photo)"))}
        if "description" not in cols:
            conn.execute(text("ALTER TABLE photo ADD COLUMN description TEXT DEFAULT ''"))


def get_session():
    with Session(engine) as session:
        yield session
