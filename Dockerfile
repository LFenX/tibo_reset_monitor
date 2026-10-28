FROM python:3.12-slim

WORKDIR /app
COPY src /app/src
COPY config.example.json /app/config.example.json

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV TIBO_STATE_PATH=/data/monitor.sqlite3

RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /data /config \
    && chown -R monitor:monitor /data /config

USER monitor
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "tibo_reset_monitor"]
CMD ["run", "--config", "/config/config.json"]
