# Brennan B3+ UI — server edition
FROM python:3.12-slim

# iproute2: lets the launcher see the real subnet (e.g. a /22) for discovery
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY brennan.py artwork.py ./
COPY ui/ ./ui/

ENV BRENNAN_HOST=0.0.0.0 \
    BRENNAN_PORT=8765 \
    BRENNAN_CONFIG=/data/brennan_ui.json \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('BRENNAN_PORT','8765')+'/healthz', timeout=4)"

# Runs as an unprivileged user; /data must be writable by it
RUN useradd --system --uid 10001 brennan && mkdir -p /data && chown brennan /data
USER brennan

CMD ["python", "brennan.py"]
