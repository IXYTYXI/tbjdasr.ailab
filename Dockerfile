FROM bluenviron/mediamtx:1.21.0 AS media
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements.lock /app/
RUN pip install --no-cache-dir -r requirements.lock
COPY --from=media /mediamtx /usr/local/bin/mediamtx
COPY app /app/app
COPY media_entry.py segment_hook.py init_config.py recover_recordings.py /app/
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8088", "--no-access-log"]
