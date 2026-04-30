from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, ENV_FILE_PATH, env_presence_map, settings
from app.database import Base, DATABASE_URL, engine
from app.routers import dashboard, discount_requests, price_monitor, reviews, tasks
from app.services.scheduler import start_scheduler, stop_scheduler


logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB and background jobs on startup."""
    logger.info(
        "Старт приложения. cwd=%s base_dir=%s env_file=%s env_exists=%s database_url=%s",
        os.getcwd(),
        BASE_DIR,
        ENV_FILE_PATH,
        ENV_FILE_PATH.exists(),
        DATABASE_URL,
    )
    logger.info("Диагностика env-переменных: %s", env_presence_map())
    Base.metadata.create_all(bind=engine)
    price_monitor.ensure_price_monitor_schema()
    yield
    stop_scheduler()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
app.include_router(tasks.router)
app.include_router(reviews.router)
app.include_router(discount_requests.router)
app.include_router(price_monitor.router)
app.include_router(price_monitor.product_router)


@app.on_event("startup")
def startup_event():
    print("STARTING SCHEDULER...")
    start_scheduler()
    logger.info("Scheduler запущен")
