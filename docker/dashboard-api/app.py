import json
import logging
import os
import time

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Dashboard API", version="1.0.0")

CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
request_duration_seconds = Histogram(
    "request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
)
cache_hits_total = Counter("cache_hits_total", "Redis cache hits")
cache_misses_total = Counter("cache_misses_total", "Redis cache misses")

db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None
db_healthy: bool = False
redis_healthy: bool = False


@app.on_event("startup")
async def startup() -> None:
    global db_pool, redis_client, db_healthy, redis_healthy
    try:
        db_pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "timescaledb"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "aqi"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD"),
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
        db_healthy = True
        logger.info("DB pool ready")
    except Exception as exc:
        logger.error("DB startup failed: %s", exc)

    try:
        redis_url = (
            f"redis://{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}"
        )
        redis_client = aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        redis_healthy = True
        logger.info("Redis ready")
    except Exception as exc:
        logger.error("Redis startup failed: %s", exc)


@app.on_event("shutdown")
async def shutdown() -> None:
    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.aclose()


@app.get("/aqi")
async def get_aqi():
    start = time.time()
    cache_key = "aqi:bangkok:latest"

    try:
        if redis_client and redis_healthy:
            cached = await redis_client.get(cache_key)
            if cached:
                cache_hits_total.inc()
                http_requests_total.labels(
                    method="GET", endpoint="/aqi", status="200"
                ).inc()
                return json.loads(cached)
            cache_misses_total.inc()

        if not db_pool or not db_healthy:
            raise HTTPException(status_code=503, detail="Database unavailable")

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT district,
                       AVG(pm25)  AS avg_pm25,
                       MAX(time)  AS last_reading
                FROM   sensor_readings
                WHERE  time > NOW() - INTERVAL '1 hour'
                GROUP  BY district
                ORDER  BY district
            """)

        result = [
            {
                "district": r["district"],
                "pm25": round(r["avg_pm25"], 2),
                "last_reading": (
                    r["last_reading"].isoformat() if r["last_reading"] else None
                ),
            }
            for r in rows
        ]

        if redis_client and redis_healthy:
            await redis_client.setex(cache_key, CACHE_TTL, json.dumps(result))

        http_requests_total.labels(method="GET", endpoint="/aqi", status="200").inc()
        return result

    except HTTPException:
        http_requests_total.labels(
            method="GET", endpoint="/aqi", status="503"
        ).inc()
        raise
    except Exception as exc:
        logger.error("GET /aqi failed: %s", exc)
        http_requests_total.labels(
            method="GET", endpoint="/aqi", status="500"
        ).inc()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        request_duration_seconds.labels(endpoint="/aqi").observe(time.time() - start)


@app.get("/healthz")
async def healthz():
    return {"status": "alive"}


@app.get("/ready")
async def ready():
    if not db_healthy or not redis_healthy:
        raise HTTPException(
            status_code=503,
            detail=f"Not ready — db={db_healthy} redis={redis_healthy}",
        )
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await redis_client.ping()
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
