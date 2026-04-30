import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import BASE_DIR, settings


logger = logging.getLogger("app.database")


def _resolve_database_url(raw_url: str) -> str:
    """Normalize sqlite DATABASE_URL and ensure the data directory exists."""
    if raw_url.startswith("sqlite:///./"):
        relative_path = raw_url.replace("sqlite:///./", "", 1)
        absolute_path = (BASE_DIR / Path(relative_path)).resolve()
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_url = f"sqlite:///{absolute_path.as_posix()}"
        logger.info("DATABASE_URL normalized: raw=%s resolved=%s", raw_url, resolved_url)
        return resolved_url

    if raw_url.startswith("sqlite:////"):
        absolute_path = Path(raw_url.replace("sqlite:////", "/", 1))
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        return raw_url

    return raw_url


DATABASE_URL = _resolve_database_url(settings.DATABASE_URL)

print("[DB] DATABASE_URL=", settings.DATABASE_URL)
print("[DB] data dir exists:", os.path.exists("/app/data"))
print("[DB] app.db exists:", os.path.exists("/app/data/app.db"))
print("[DB] absolute db path:", os.path.abspath("./data/app.db"))


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency for a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
