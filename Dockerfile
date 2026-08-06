FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for ffsubsync audio alignment
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg cifs-utils && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    fastapi>=0.100 \
    httpx>=0.24 \
    jinja2>=3.1 \
    langdetect>=1.0 \
    pysubs2>=1.7 \
    python-multipart>=0.0.6 \
    pyyaml>=6.0 \
    uvicorn>=0.22 \
    ffsubsync>=0.4 \
    beautifulsoup4>=4.12 \
    lxml>=5.0

# Copy application
COPY src/ src/
COPY templates/ templates/
COPY static/ static/

EXPOSE 8676

# Single worker — the app keeps scan state, config cache, and locks in memory.
# Multiple workers give each process its own copy, causing duplicate scans,
# stale config, and dead locks (no shared state). Single user + async = 1 worker.
CMD ["uvicorn", "src.subber.web:app", "--host", "0.0.0.0", "--port", "8676", "--workers", "1"]
