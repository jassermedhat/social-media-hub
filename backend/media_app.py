from fastapi import FastAPI

from api.media import router as media_router


# This is intentionally a separate, minimal FastAPI application.
# Cloudflare points ONLY to this service, not to the main API.
app = FastAPI(
    title="IPS Temporary Media Server",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(media_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "temporary-media",
    }
