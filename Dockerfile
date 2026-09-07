# One image serving both the API and the page, so the frontend's default
# same-origin API calls work without extra configuration.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/app backend/app
RUN pip install --no-cache-dir ./backend

COPY index.html ./frontend/index.html
COPY js ./frontend/js
ENV FRONTEND_DIR=/srv/frontend

# Cloud Run supplies $PORT; bind it or the revision never becomes ready.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
