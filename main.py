from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import uvicorn
import logging

from app.core import settings
from app.routers import base_router
from app.utils.notification_subscriber import start_notification_subscriber

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan events"""
    logger.info("Starting application...")
    
    subscriber_task = None
    if settings.REDIS_URL:
        try:
            subscriber_task = asyncio.create_task(start_notification_subscriber())
            logger.info("Notification subscriber task started")
        except Exception as e:
            logger.error(f"Failed to start notification subscriber: {e}")
    
    yield
    
    logger.info("Shutting down application...")
    if subscriber_task:
        subscriber_task.cancel()
        try:
            await subscriber_task
        except asyncio.CancelledError:
            logger.info("Notification subscriber task cancelled")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8002", "http://localhost:3000", "https://hex-teams-frontend.vercel.app", "http://194.195.119.112:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(base_router, prefix="/api")

@app.get("/")
def read_root():
    return {
        "message": "Hello from HexoTeams API!"
    }

@app.get("/health")
def health_check():
    return {
        "message": "HexoTeams API is running!",
        "status": "healthy"
    }
    


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.APP_PORT,
        reload=True
    )