FROM python:3.12-slim

WORKDIR /app
COPY src /app/src
COPY config.example.json /app/config.example.json

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV TIBO_STATE_PATH=/data/monitor.sqlite3

# Calendar images need Pillow and a CJK font. Without them, Telegram still sends one text message.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && pip install --no-cache-dir 'Pillow>=10' \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /data /config \
    && chown -R monitor:monitor /data /config

USER monitor
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "tibo_reset_monitor"]
CMD ["run", "--config", "/config/config.json"]
