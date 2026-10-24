FROM python:3.12-slim

# FastAPI spools large multipart uploads to the system temp directory.  Keep
# that on the persistent migration volume instead of the container layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OSTENSORY_DATA_DIR=/data \
    OSTENSORY_HOST=0.0.0.0 \
    OSTENSORY_PORT=8000 \
    OSTENSORY_MIGRATION_STATE_DIR=/app/migration-backups \
    TMPDIR=/app/migration-backups/tmp

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/
RUN mkdir -p /data /app/migration-backups/tmp && chown -R 10001:10001 /data /app
USER 10001:10001

EXPOSE 8000
VOLUME ["/data", "/app/migration-backups"]
CMD ["python", "run.py"]