FROM python:3.12-slim

WORKDIR /app

# System deps for some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY universes ./universes

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV STATE_DIR=/data/.nse_alert
VOLUME ["/data"]

# Login UI (optional; open SG only when running `nse-alert login`)
EXPOSE 8765

CMD ["nse-alert", "watch"]
