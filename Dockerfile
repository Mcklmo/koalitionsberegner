# One image serving both the API and the page, so the frontend's default
# same-origin API calls work without extra configuration.
# Same interpreter as backend/.python-version, which is copied in below so the
# build fails if these two ever disagree.
FROM python:3.13-slim

# Pinned by digest-bearing tag: the installer is part of the build's
# reproducibility, not something to pick up fresh on every rebuild.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv/backend

# Dependencies first, without the project itself: this layer is keyed on the
# lockfile alone, so editing app code does not re-resolve or re-download
# anything. --locked asserts the lockfile still matches pyproject.toml and
# fails the build otherwise, which is the whole reason for committing one.
# .python-version comes along for the same reason: with UV_PYTHON_DOWNLOADS=never
# uv must find that exact interpreter in the base image, so bumping the pin
# without bumping FROM stops here rather than in a revision.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend/app ./app
RUN uv sync --locked --no-dev

# Put the environment on PATH so the CMD below needs no uv wrapper.
ENV PATH="/srv/backend/.venv/bin:$PATH"

COPY index.html /srv/frontend/index.html
COPY js /srv/frontend/js
ENV FRONTEND_DIR=/srv/frontend

# Cloud Run supplies $PORT; bind it or the revision never becomes ready.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
