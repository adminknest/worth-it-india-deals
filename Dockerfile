FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEALBOT_DATABASE=/data/deals.db

RUN useradd --create-home --uid 10001 app && mkdir -p /app /data && chown -R app:app /app /data
WORKDIR /app

COPY --chown=app:app dealbot.py earnkaro_intake.py operator_bot.py cuelinks_client.py ./
COPY --chown=app:app config.production.json ./config.production.json

USER app
CMD ["python", "operator_bot.py", "--config", "config.production.json", "--auto-publish"]
