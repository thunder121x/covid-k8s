import asyncio
import logging
import os
import time

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Data Aggregator", version="1.0.0")

aggregation_runs_total = Counter(
    "aggregation_runs_total", "Total aggregation worker runs", ["status"]
)
aqi_computed_value = Gauge(
    "aqi_computed_value", "Latest computed AQI index", ["district"]
)
last_aggregation_timestamp = Gauge(
    "last_aggregation_timestamp", "Unix timestamp of last successful aggregation"
)

db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


def pm25_to_aqi(pm25: float) -> int:
    """US EPA AQI linear interpolation for PM2.5 (µg/m³)."""
    breakpoints = [
        (0.0, 12.0, 0, 50),
        (12.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ]
    for bp_lo, bp_hi, aqi_lo, aqi_hi in breakpoints:
        if bp_lo <= pm25 <= bp_hi:
            return round(
                (aqi_hi - aqi_lo) / (bp_hi - bp_lo) * (pm25 - bp_lo) + aqi_lo
            )
    return 500


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS aqi_computed (
                time      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
                district  TEXT             NOT NULL,
                pm25_avg  DOUBLE PRECISION NOT NULL,
                aqi_index INTEGER          NOT NULL
            );
        """)
        try:
            await conn.execute(
                "SELECT create_hypertable('aqi_computed', 'time', if_not_exists => TRUE);"
            )
        except Exception as exc:
            logger.warning("Hypertable init skipped: %s", exc)


async def run_aggregation() -> None:
    while True:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT district, AVG(pm25) AS avg_pm25
                    FROM   sensor_readings
                    WHERE  time > NOW() - INTERVAL '1 hour'
                    GROUP  BY district
                """)

                for row in rows:
                    district = row["district"]
                    avg_pm25 = float(row["avg_pm25"])
                    aqi = pm25_to_aqi(avg_pm25)

                    await conn.execute(
                        """
                        INSERT INTO aqi_computed (time, district, pm25_avg, aqi_index)
                        VALUES (NOW(), $1, $2, $3)
                        """,
                        district,
                        avg_pm25,
                        aqi,
                    )
                    aqi_computed_value.labels(district=district).set(aqi)

            if redis_client:
                await redis_client.delete("aqi:bangkok:latest")

            last_aggregation_timestamp.set(time.time())
            aggregation_runs_total.labels(status="success").inc()
            logger.info("Aggregation complete for %d districts", len(rows))

        except Exception as exc:
            aggregation_runs_total.labels(status="error").inc()
            logger.error("Aggregation error: %s", exc)

        await asyncio.sleep(60)


@app.on_event("startup")
async def startup() -> None:
    global db_pool, redis_client
    db_pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "timescaledb"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "aqi"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
        min_size=2,
        max_size=5,
        command_timeout=10,
    )
    await init_schema(db_pool)

    try:
        redis_url = (
            f"redis://{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}"
        )
        redis_client = aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        logger.info("Redis ready")
    except Exception as exc:
        logger.warning("Redis unavailable, cache invalidation disabled: %s", exc)

    asyncio.create_task(run_aggregation())
    logger.info("Aggregation worker started")


@app.on_event("shutdown")
async def shutdown() -> None:
    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def healthz():
    return {"status": "alive"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
