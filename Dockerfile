FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md LICENSE ./
COPY coordinator ./coordinator
COPY worker ./worker
COPY projectzero ./projectzero
RUN pip install --no-cache-dir . && groupadd --system app && useradd --system --gid app app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8765
CMD ["uvicorn", "coordinator.app.main:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
