FROM python:3.12.4-slim@sha256:a3e58f9399353be051735f09be0316bfdeab571a5c6a24fd78b92df85bcb2d85

ENV CONCURRENCY_LIMIT=10 \
    SERVICE_TIME_MS=100 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY targets/synthetic/app.py /app/app.py

EXPOSE 8000
CMD ["python", "-u", "app.py"]
