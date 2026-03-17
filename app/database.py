from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings


# SQLite needs this flag so the app can reuse the same DB in web requests and scheduler jobs.
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
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
