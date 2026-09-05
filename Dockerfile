FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py songs.py ./

# /data is the only writable state: library.json, the song index and the
# album page cache. Mount a volume there or everything is rebuilt on
# every container start (24MB + 33MB of downloads and a ~30s index build).
ENV PORT=8080 \
    CACHE_DIR=/data/cache \
    LIBRARY_PATH=/data/library.json \
    SONGS_DB=/data/songs.sqlite

VOLUME /data
EXPOSE 8080

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
