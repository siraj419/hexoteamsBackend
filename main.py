from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


from app.core import settings
from app.routers import base_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8002", "http://localhost:3000", "hex-teams-frontend.vercel.app"],
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