FROM bluenviron/mediamtx:1.21.0 AS media
FROM mwader/static-ffmpeg:6.1.1 AS ffmpeg
FROM python:3.13-slim-bookworm
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY requirements.txt requirements.lock /app/
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.lock
COPY --from=media /mediamtx /usr/local/bin/mediamtx
COPY app /app/app
COPY media_entry.py segment_hook.py init_config.py recover_recordings.py /app/
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8088", "--no-access-log"]
