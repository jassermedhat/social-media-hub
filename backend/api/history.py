from fastapi import APIRouter

from database.db import get_publish_history


router = APIRouter(
    prefix="/history",
    tags=["History"],
)


@router.get("")
def publishing_history():
    return {
        "items": get_publish_history(),
    }