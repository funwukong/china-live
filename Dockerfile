FROM python:3.12-slim

LABEL org.opencontainers.image.title="china-live"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=8577 \
    HOST=0.0.0.0

EXPOSE 8577

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8577')+'/health', timeout=5).read() else 1)" || exit 1

WORKDIR /app
CMD ["python", "-u", "/app/server.py"]
