FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY policies ./policies
RUN pip install . && useradd --uid 10001 --no-create-home gateway
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["sh", "-c", "uvicorn aigw.main:app --host 0.0.0.0 --port 8080 --workers ${AIGW_WORKERS:-2} --no-access-log"]
