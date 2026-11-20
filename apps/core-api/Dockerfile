# Vigilantis Core API 이미지 (uv 기반) — 운영 기동이 기본, 개발 스택은 compose가 명령을 덮는다
# build context는 저장소 루트(.)여야 합니다 (packages/*, uv.lock 참조).
FROM python:3.11-slim

WORKDIR /app
# uv는 공식 이미지에서 버전과 digest를 고정해 가져온다(#673). 올릴 때는 태그와 digest를 함께 바꾸고,
# CI의 setup-uv version(.github/workflows/ci.yml)도 같은 값으로 맞춘다.
COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /uvx /bin/

# 워크스페이스 메타데이터 + 공통 패키지 + core-api 복사
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps/core-api ./apps/core-api

# 의존성 동기화 — uv.lock 그대로만 설치한다(#673).
# --locked: pyproject.toml 선언과 uv.lock이 어긋나면 재해석하지 않고 빌드를 실패시킨다.
# 의존성을 바꿨으면 호스트에서 uv lock을 돌려 uv.lock을 함께 커밋한다.
# --all-packages: core-api 등 워크스페이스 멤버 의존성(fastapi/uvicorn 등)을
# 빌드 시점에 설치한다. 없으면 컨테이너 시작 시 uv run이 뒤늦게 설치해 기동이 느려짐.
RUN uv sync --locked --all-packages

WORKDIR /app/apps/core-api
EXPOSE 8000
# 운영 기동(ADR-0010): worker 1개 · --reload 없음 · 관제 대상 접근 역할 프로필 준비(serve.py).
# --no-sync: 기동 시 재설치하지 않는다(네트워크 불필요). compose 개발 스택은
# docker-compose.yml의 command가 --reload 명령으로 덮으며, 그쪽은 재설치를 허용한다.
CMD ["uv", "run", "--no-sync", "python", "serve.py"]
