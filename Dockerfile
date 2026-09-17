# 매홍 L&F 대시보드 — Fly.io 컨테이너
FROM python:3.12-slim

# 빌드 도구 (firebase-admin/grpc 등 일부 패키지 대비)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 경로/포트 환경변수 (코드의 BASE_DIR/DATA_DIR/PORT가 이 값을 사용)
ENV APP_BASE_DIR=/app \
    DATA_DIR=/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 복사 (.dockerignore로 data/·시크릿·로그 제외)
COPY . .

# 데이터 볼륨 마운트 지점
RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "app.py"]
