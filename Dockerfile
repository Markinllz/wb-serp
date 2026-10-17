FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home app \
    && mkdir -p /app/data && chown -R app:app /app/data

FROM runtime AS browser-deps
COPY requirements-browser.txt .
RUN pip install --no-cache-dir -r requirements-browser.txt

FROM runtime AS app
COPY wb_serp ./wb_serp
COPY config ./config
COPY .streamlit ./.streamlit
COPY scripts ./scripts
USER app

CMD ["python", "-m", "wb_serp.cli", "worker"]

FROM python:3.12-slim AS metrics
WORKDIR /app
COPY requirements-monitoring.txt .
RUN pip install --no-cache-dir -r requirements-monitoring.txt
COPY wb_serp/metrics.py /app/metrics.py
USER 10001:10001
CMD ["python", "-u", "/app/metrics.py"]

FROM browser-deps AS browser
COPY --from=app /app /app
WORKDIR /home/app
ENV PYTHONPATH=/app
USER app
CMD ["python", "-m", "wb_serp.cli", "worker"]
