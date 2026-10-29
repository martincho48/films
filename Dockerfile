FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Dependencies first, so code changes do not reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project --no-cache

# main.py reads the version from pyproject.toml; .streamlit/ is read from the working directory
COPY .streamlit/ .streamlit/
COPY app/ app/

# Cloud Run sends traffic to port 8080
EXPOSE 8080
CMD ["streamlit", "run", "app/main.py", "--server.port=8080", "--server.address=0.0.0.0"]
