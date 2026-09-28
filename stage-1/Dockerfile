# stage-1/Dockerfile
# ═══════════════════
# Build pocketful-settlement stage-1 in clean container
# 
# Builds:
#   1. C ledger (ledger.c → ledger.so)
#   2. Python API (FastAPI + ctypes)
# 
# No outbound network (judges requirement)
# Self-contained: ledger.so embedded in Python path

FROM python:3.11-slim

LABEL maintainer="Landi (@DRockLandi)"
LABEL description="Pocketful Settlement API - BAND-driven atomic transfers"
LABEL version="1.0.0"

WORKDIR /app

# ═══════════════════════════════════════════════════════════════════════════
# INSTALL BUILD TOOLS (for compiling C ledger)
# ═══════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# ═══════════════════════════════════════════════════════════════════════════
# COPY SOURCE CODE
# ═══════════════════════════════════════════════════════════════════════════

# C ledger
COPY ledger/ ./ledger/

# Python API
COPY src/ ./src/
# or:
# COPY api/ ./api/

# Python requirements
COPY requirements.txt .

# ═══════════════════════════════════════════════════════════════════════════
# BUILD C LEDGER
# ═══════════════════════════════════════════════════════════════════════════

RUN cd ledger && \
    gcc -fPIC -pthread -c ledger.c -o ledger.o && \
    gcc -shared -pthread ledger.o -o ledger.so && \
    echo "✓ ledger.so built successfully" && \
    ls -lh ledger.so

# ═══════════════════════════════════════════════════════════════════════════
# INSTALL PYTHON DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════

RUN pip install --no-cache-dir -r requirements.txt && \
    echo "✓ Python dependencies installed"

# ═══════════════════════════════════════════════════════════════════════════
# RUNTIME SETUP
# ═══════════════════════════════════════════════════════════════════════════

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV LD_LIBRARY_PATH=/app/ledger:$LD_LIBRARY_PATH
ENV PYTHONPATH=/app/src:$PYTHONPATH

# Non-root user (security)
RUN useradd -m -u 1000 pocketful
USER pocketful

# ═══════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# ═══════════════════════════════════════════════════════════════════════════
# EXPOSE PORT
# ═══════════════════════════════════════════════════════════════════════════

EXPOSE 8000

# ═══════════════════════════════════════════════════════════════════════════
# START API SERVER
# ═══════════════════════════════════════════════════════════════════════════

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
# Or if using api/ instead of src/:
# CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ═══════════════════════════════════════════════════════════════════════════
# BUILD & RUN
# ═══════════════════════════════════════════════════════════════════════════
#
# Build:
#   docker build -t pocketful:stage1 .
#
# Run locally:
#   docker run -p 8000:8000 pocketful:stage1
#
# Test:
#   curl http://localhost:8000/health
#   curl -X POST http://localhost:8000/transfer \
#     -H "Content-Type: application/json" \
#     -d '{
#       "sender_id": "alice",
#       "receiver_id": "bob",
#       "amount_dollars": 50.00,
#       "txn_id": "txn_001"
#     }'
#
# Inspect:
#   docker run -it pocketful:stage1 /bin/bash
#
# Logs:
#   docker logs <container_id> -f
