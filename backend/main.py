from contextlib import asynccontextmanager

from api.accounts import router as accounts_router
from api.history import router as history_router
from api.posts import router as posts_router
from api.youtube import router as youtube_router
from database.db import init_db
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="IPS Social Media Publisher API",
    version="0.1.0",
    lifespan=lifespan,
)


app.include_router(accounts_router)
app.include_router(youtube_router)
app.include_router(history_router)
app.include_router(posts_router)


@app.get("/")
def root():
    return {
        "name": "IPS Social Media Publisher API",
        "status": "running",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
    }
