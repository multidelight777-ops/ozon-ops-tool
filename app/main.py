from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, engine
from app.routers import dashboard, reviews, tasks
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.telegram_bot import start_telegram_bot, stop_telegram_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB and background jobs on startup."""
    Base.metadata.create_all(bind=engine)
    start_telegram_bot()
    start_scheduler()
    yield
    stop_scheduler()
    stop_telegram_bot()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
app.include_router(tasks.router)
app.include_router(reviews.router)
