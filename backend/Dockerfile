FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app

# Cloud Run supplies $PORT; bind it or the revision never becomes ready.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
