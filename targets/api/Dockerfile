FROM python:3.12.4-slim@sha256:a3e58f9399353be051735f09be0316bfdeab571a5c6a24fd78b92df85bcb2d85

ENV SERVICE_TIME_MS=50 \
    POOL_TIMEOUT_S=30 \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY targets/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY targets/api/app.py /app/app.py

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn --factory app:create_app --workers 1 --host 0.0.0.0 --port \"$PORT\" --no-access-log --log-level warning"]
