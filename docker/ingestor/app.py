import asyncio
import logging
import os
import time

import asyncpg
import httpx
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

BANGKOK_STATIONS = [
    {"station_id": "@7397",  "district": "Chatuchak"},
    {"station_id": "@7398",  "district": "Din Daeng"},
    {"station_id": "@7399",  "district": "Pathum Wan"},
    {"station_id": "@9279",  "district": "Bang Na"},
    {"station_id": "@10082", "district": "Bangkok"},
]

WAQI_TOKEN = os.getenv("WAQI_TOKEN", "")
WAQI_BASE  = "https://api.waqi.info/feed"

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

async def fetch_station_pm25(client: httpx.AsyncClient, station_id: str) -> float | None:
    try:
        url = f"{WAQI_BASE}/{station_id}/?token={WAQI_TOKEN}"
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            logger.warning("WAQI status not ok for %s: %s", station_id, data)
            return None

        pm25 = data["data"]["iaqi"].get("pm25", {}).get("v")
        return float(pm25) if pm25 is not None else None

    except Exception as exc:
        logger.error("Failed to fetch station %s: %s", station_id, exc)
        return None

@app.post("/fetch-bangkok", status_code=200)
async def fetch_bangkok():
    if not db_healthy or db_pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not WAQI_TOKEN:
        raise HTTPException(status_code=500, detail="WAQI_TOKEN not configured")

    results = []
    start = time.time()

    async with httpx.AsyncClient() as client:
        for station in BANGKOK_STATIONS:
            pm25 = await fetch_station_pm25(client, station["station_id"])
            if pm25 is None:
                continue

            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO sensor_readings (time, district, station_id, pm25)
                        VALUES (NOW(), $1, $2, $3)
                        """,
                        station["district"],
                        station["station_id"],
                        pm25,
                    )
                aqi_last_ingest_timestamp.set(time.time())
                ingest_requests_total.labels(status="success").inc()
                results.append({"district": station["district"], "pm25": pm25})
                logger.info("INFO: Inserted %s → PM2.5=%.1f", station["district"], pm25)

            except Exception as exc:
                ingest_requests_total.labels(status="error").inc()
                logger.error("DB insert failed for %s: %s", station["district"], exc)

    ingest_duration_seconds.observe(time.time() - start)
    return {"fetched": len(results), "stations": results}

async def auto_fetch_loop() -> None:
    await asyncio.sleep(10)
    while True:
        logger.info("INFO: Auto-fetching Bangkok AQI data")
        try:
            async with httpx.AsyncClient() as client:
                for station in BANGKOK_STATIONS:
                    pm25 = await fetch_station_pm25(client, station["station_id"])
                    if pm25 is None:
                        continue
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO sensor_readings (time, district, station_id, pm25) VALUES (NOW(), $1, $2, $3)",
                            station["district"], station["station_id"], pm25,
                        )
                    aqi_last_ingest_timestamp.set(time.time())
                    ingest_requests_total.labels(status="success").inc()
        except Exception as exc:
            ingest_requests_total.labels(status="error").inc()
            logger.error("Auto-fetch error: %s", exc)

        await asyncio.sleep(300)  # 5 min

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
        # logger.info("INFO: Database pool ready")
    except Exception as exc:
        logger.error("DB startup failed: %s", exc)
        db_healthy = False

    asyncio.create_task(auto_fetch_loop())


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
                "INSERT INTO sensor_readings (time, district, station_id, pm25) VALUES (to_timestamp($1), $2, $3, $4)",
                ts, reading.district, reading.station_id, reading.pm25,
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