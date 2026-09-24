# Second Look: one small image, non-root, data on a volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/triage.db LOG_LEVEL=INFO ALLOWED_HOSTS=localhost,127.0.0.1
WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY app ./app
COPY data/sample_cases_synthetic.csv ./data/sample_cases_synthetic.csv
COPY data/recorded_review.json ./data/recorded_review.json
COPY serve.py ./

RUN useradd --create-home --uid 10001 jai && mkdir -p /data && chown jai /data
USER jai
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"

# TLS is expected at the load balancer in a deployment; serve.py adds a self-signed listener for laptops.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
