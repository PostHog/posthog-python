#!/usr/bin/env python3

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from posthog import AsyncPosthog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY", "phc_PLACEHOLDER_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")
POSTHOG_SECRET_KEY = os.getenv("POSTHOG_SECRET_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncPosthog(
        POSTHOG_API_KEY,
        host=POSTHOG_HOST,
        secret_key=POSTHOG_SECRET_KEY,
        log_captured_exceptions=True,
    ) as client:
        app.state.posthog = client
        yield


app = FastAPI(title="PostHog Async Client Playground", lifespan=lifespan)


def get_posthog(request: Request) -> AsyncPosthog:
    return request.app.state.posthog


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exception: Exception):
    capture_id = get_posthog(request).capture_exception(
        exception,
        distinct_id="anonymous",
        properties={"url": str(request.url)},
    )
    logger.info("Captured %s as %s", type(exception).__name__, capture_id)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "capture_id": capture_id},
    )


@app.get("/")
async def root():
    return {
        "message": "PostHog Async Client Playground",
        "docs": "/docs",
    }


@app.post("/capture/{distinct_id}")
async def capture(request: Request, distinct_id: str):
    capture_id = get_posthog(request).capture(
        "async buffered capture",
        distinct_id=distinct_id,
        properties={"source": "fastapi-playground"},
    )
    return {"capture_id": capture_id, "delivery": "queued"}


@app.post("/capture-immediate/{distinct_id}")
async def capture_immediate(request: Request, distinct_id: str):
    capture_id = await get_posthog(request).capture_immediate(
        "async immediate capture",
        distinct_id=distinct_id,
        properties={"source": "fastapi-playground"},
    )
    return {"capture_id": capture_id, "delivery": "attempted"}


@app.post("/identify/{distinct_id}")
async def identify(request: Request, distinct_id: str):
    client = get_posthog(request)
    set_id = client.set(
        distinct_id=distinct_id,
        properties={"playground": "fastapi-async-client"},
    )
    set_once_id = client.set_once(
        distinct_id=distinct_id,
        properties={"first_seen_in_playground": True},
    )
    return {"set_id": set_id, "set_once_id": set_once_id}


@app.post("/group/{group_key}")
async def group_identify(request: Request, group_key: str):
    capture_id = get_posthog(request).group_identify(
        "company",
        group_key,
        properties={"source": "fastapi-playground"},
    )
    return {"capture_id": capture_id}


@app.post("/alias")
async def alias(request: Request, previous_id: str, distinct_id: str):
    capture_id = get_posthog(request).alias(previous_id, distinct_id)
    return {"capture_id": capture_id}


@app.get("/flags/{distinct_id}")
async def evaluate_flags(
    request: Request,
    distinct_id: str,
    flag_key: str = "async-client-demo",
):
    client = get_posthog(request)
    flags = await client.evaluate_flags(distinct_id)
    value = flags.get_flag(flag_key)
    payload = flags.get_flag_payload(flag_key)

    client.capture(
        "async flag evaluated",
        distinct_id=distinct_id,
        properties={"flag_key": flag_key},
        flags=flags,
    )
    return {"flag_key": flag_key, "value": value, "payload": payload}


@app.get("/remote-config/{key}")
async def remote_config(request: Request, key: str):
    payload = await get_posthog(request).get_remote_config_payload(key)
    return {"key": key, "payload": payload}


@app.post("/flush")
async def flush(request: Request):
    await get_posthog(request).flush()
    return {"flushed": True}


@app.get("/test-exception")
async def test_exception():
    raise RuntimeError("TEST ASYNC BACKEND EXCEPTION")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
