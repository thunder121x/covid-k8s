import asyncio
import logging
import os
import time

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AQI Ingestor", version="1.0.0")

ingest_requests_total = Counter(
    "ingest_requests_total", "Total ingest requests", ["status"]
)
ingest_duration_seconds = Histogram(
    "ingest_duration_seconds", "Ingest request duration in seconds"
)
aqi_last_ingest_timestamp = Gauge(
    "aqi_last_ingest_timestamp", "Unix timestamp of last successful sensor ingest"
)

db_pool: asyncpg.Pool | None = None
db_healthy: bool = False


class SensorReading(BaseModel):
    district: str
    station_id: str
    pm25: float
    timestamp: float | None = None


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                time        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
                district    TEXT             NOT NULL,
                station_id  TEXT             NOT NULL,
                pm25        DOUBLE PRECISION NOT NULL
            );
        """)
        try:
            await conn.execute(
                "SELECT create_hypertable('sensor_readings', 'time', if_not_exists => TRUE);"
            )
        except Exception as exc:
            logger.warning("Hypertable init skipped: %s", exc)


@app.on_event("startup")
async def startup() -> None:
    global db_pool, db_healthy
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
        await init_schema(db_pool)
        db_healthy = True
        logger.info("Database pool ready")
    except Exception as exc:
        logger.error("DB startup failed: %s", exc)
        db_healthy = False


@app.on_event("shutdown")
async def shutdown() -> None:
    if db_pool:
        await db_pool.close()


@app.post("/ingest", status_code=201)
async def ingest(reading: SensorReading):
    if not db_healthy or db_pool is None:
        ingest_requests_total.labels(status="error").inc()
        raise HTTPException(status_code=503, detail="Database unavailable")

    ts = reading.timestamp or time.time()
    start = time.time()
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sensor_readings (time, district, station_id, pm25)
                VALUES (to_timestamp($1), $2, $3, $4)
                """,
                ts,
                reading.district,
                reading.station_id,
                reading.pm25,
            )
        aqi_last_ingest_timestamp.set(time.time())
        ingest_requests_total.labels(status="success").inc()
        return {"status": "ok", "district": reading.district, "pm25": reading.pm25}
    except Exception as exc:
        ingest_requests_total.labels(status="error").inc()
        logger.error("Ingest failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        ingest_duration_seconds.observe(time.time() - start)


@app.get("/healthz")
async def healthz():
    return {"status": "alive"}


@app.get("/ready")
async def ready():
    if not db_healthy or db_pool is None:
        raise HTTPException(status_code=503, detail="DB pool not initialised")
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB check failed: {exc}")


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
