# ---------------------------------------------------------------------------
# Backend + MCP server. The frontend is built separately and served by nginx.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the shared document tooling first so the layer caches across code edits.
COPY mcp-office-server/pyproject.toml mcp-office-server/README.md mcp-office-server/LICENSE mcp-office-server/NOTICE.md ./mcp-office-server/
COPY mcp-office-server/src ./mcp-office-server/src
RUN pip install --upgrade pip && pip install ./mcp-office-server

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend ./backend

# The MCP server is spawned as a subprocess by the backend, so the sandbox root
# has to exist inside the image.
RUN mkdir -p /app/data/workspaces /app/data/backups /app/data/qdrant

ENV PYTHONPATH=/app/backend \
    DATA_DIR=/app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"

WORKDIR /app/backend
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
