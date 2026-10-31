FROM python:3.12-slim

LABEL org.opencontainers.image.title="lyrics-sync" \
      org.opencontainers.image.description="Fetch synced .lrc lyrics for every track in a music library (Navidrome, Plex, Jellyfin, Emby)" \
      org.opencontainers.image.source="https://github.com/ayumpo/lyrics-sync" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSIC_DIR=/music \
    STATE_DIR=/config \
    INTERVAL_HOURS=24

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.txt

COPY lyrics_sync.py /app/lyrics_sync.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh /app/lyrics_sync.py

VOLUME ["/music", "/config"]
ENTRYPOINT ["/entrypoint.sh"]
