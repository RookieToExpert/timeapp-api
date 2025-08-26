FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# 非 root 运行（安全）
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD \
  python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz').read()" || exit 1

# 监听 0.0.0.0，端口可被 $PORT 覆盖（Container Apps/容器平台友好）
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT}"]