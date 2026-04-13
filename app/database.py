import logging
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import BASE_DIR, settings


logger = logging.getLogger("app.database")


def _resolve_database_url(raw_url: str) -> str:
    """Нормализовать DATABASE_URL, чтобы относительный sqlite путь не зависел от cwd."""
    if raw_url.startswith("sqlite:///./"):
        relative_path = raw_url.replace("sqlite:///./", "", 1)
        absolute_path = (BASE_DIR / Path(relative_path)).resolve()
        resolved_url = f"sqlite:///{absolute_path.as_posix()}"
        logger.info("DATABASE_URL нормализован из относительного пути: raw=%s resolved=%s", raw_url, resolved_url)
        return resolved_url
    return raw_url


DATABASE_URL = _resolve_database_url(settings.DATABASE_URL)


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
