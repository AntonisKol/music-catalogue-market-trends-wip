# One image for the web API and the nightly update; deploy/compose.yml runs it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db db
COPY ingestion ingestion
COPY queries queries
COPY web web

# Settings come from the environment (deploy/compose.yml reads the server's .env),
# never from a file baked into the image.
RUN useradd --create-home --uid 1000 app && mkdir logs && chown app logs
USER app

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "web.app:create_app()"]
