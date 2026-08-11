# Vigilantis Core API — MVP 개발용 이미지 (uv 기반)
# build context는 저장소 루트(.)여야 합니다 (packages/*, uv.lock 참조).
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

# 워크스페이스 메타데이터 + 공통 패키지 + core-api 복사
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps/core-api ./apps/core-api

# 의존성 동기화 (lock 우선, 실패 시 재해석)
RUN uv sync --frozen || uv sync

WORKDIR /app/apps/core-api
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
