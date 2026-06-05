FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY snap.py serve.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENV BLINK_SESSION=/data/blink_session.json \
    BLINK_OUTPUT=/data/captures

RUN mkdir -p /data/captures

ENTRYPOINT ["./docker-entrypoint.sh"]
