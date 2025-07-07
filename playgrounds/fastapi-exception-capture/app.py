#!/usr/bin/env python3

import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from posthog import Posthog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "phc_PLACEHOLDER_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")


@app.exception_handler(Exception)
async def global_exception_handler(request, exception):
    client = Posthog(
        POSTHOG_API_KEY,
        host=POSTHOG_HOST,
        log_captured_exceptions=True,
        debug=True,
    )

    user_id = "anonymous"
    properties = {"url": str(request.url)}

    capture_id = client.capture_exception(
        exception,
        distinct_id=user_id,
        properties=properties,
    )

    logger.info(
        f"Captured exception: {exception.__class__.__name__} ({str(exception)[:100]}), id: {capture_id}"
    )

    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "capture_id": capture_id,
        },
    )


@app.get("/")
async def root():
    return {"message": "PostHog Exception Capture Playground"}


@app.get("/test-exception")
async def test_exception():
    raise Exception("TEST BACKEND EXCEPTION")


if __name__ == "__main__":
    import uvicorn

    print("Starting server...")
    print(f"PostHog Host: {POSTHOG_HOST}")
    print("\nTest: curl http://localhost:8000/test-exception")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
