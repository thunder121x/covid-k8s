FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.109.2 \
    uvicorn[standard]==0.27.1 \
    asyncpg==0.29.0 \
    redis==5.0.3 \
    prometheus-client==0.20.0

COPY app.py .

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/healthz')"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
