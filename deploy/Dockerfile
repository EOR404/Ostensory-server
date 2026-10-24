FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OSTENSORY_DATA_DIR=/data \
    OSTENSORY_HOST=0.0.0.0 \
    OSTENSORY_PORT=8000

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001:10001

EXPOSE 8000
VOLUME ["/data"]
CMD ["python", "run.py"]