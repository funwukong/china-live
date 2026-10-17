FROM python:3.12-slim

LABEL org.opencontainers.image.title="china-live"

# nodejs runs the ysp-live v9.5 WASM companion engine (4th-layer fallback).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=8577 \
    HOST=0.0.0.0 \
    CHINA_LIVE_DATA_DIR=/app/data

EXPOSE 8577

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8577')+'/health', timeout=5).read() else 1)" || exit 1

WORKDIR /app
CMD ["python", "-u", "/app/server.py"]
