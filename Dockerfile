FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 依赖先拷贝单独安装, 改代码时能命中 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 默认只起 API; 定时跑规则/评测用 docker compose run --rm monitor-api python evaluate.py
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
