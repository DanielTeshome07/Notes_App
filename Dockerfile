FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Run as a non-root user; /data is the mount point for the SQLite volume.
RUN useradd -u 10001 -m appuser && mkdir -p /data && chown -R appuser /data /app
USER appuser

EXPOSE 5000

# One process (SQLite on one ReadWriteOnce volume), but several threads so a
# long video stream or upload chunk doesn't block every other request.
# Long timeout: a slow chunk over a weak connection can take minutes.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--worker-class", "gthread", "--threads", "8", "--timeout", "300", "app:app"]
