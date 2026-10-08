# NAS image: core + garminconnect extra only (no Streamlit UI, no keyring).
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra garminconnect --no-editable

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TZ=Asia/Taipei
# The container runs as the user set in docker-compose (PUID:PGID), never root.
ENTRYPOINT ["tini", "--"]
# Stand-by until the resident `run` command exists (PR 11). Use
# `docker exec <container> garmin-tcx-ai garmin-login` and `... sync`.
CMD ["sleep", "infinity"]
