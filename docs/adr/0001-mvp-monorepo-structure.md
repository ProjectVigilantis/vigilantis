# ADR-0001: MVP는 단일 FastAPI 백엔드(core-api)로 통합한다

- **Status**: Accepted
- **Date**: 2026-08-11
- **Deciders**: 김세혁(PM/Infra), 팀 합의

## Context (배경)

초기 저장소는 풀비전(마이크로서비스) 기준으로 앱이 4개로 분리돼 있었다: `core-api`, `ai-engine`, `scan-worker`(Step Functions/Fargate), `security-soar`(Lambda). 그러나 1차 발표 MVP 범위는 다음과 같이 좁다.

- AWS **단일 계정 / 1–2개 리전 / EC2·SG 한정**
- 대용량 분산 스캔·서버리스 불필요 (Step Functions/Lambda 과함)
- 팀 규모상 서비스 간 배포·통신 오버헤드가 개발 속도를 저해

또한 MVP 착수에 필요한 **DB 계층·설정·테스트·Golden Dataset·docker-compose**가 부재했다.

## Decision (결정)

MVP 기간 동안 **단일 FastAPI 백엔드 `apps/core-api`** 로 통합한다.

- `scan-worker` → `services/collector·rule_engine·scheduler` (Step Functions 대신 **APScheduler**)
- `security-soar` → `security/soar.py` (Lambda 핸들러 형태 제거, 모의 위협 대응)
- `ai-engine` → `ai/agent·guardrails·whitelist` (GPT-4o + Pydantic Structured Output)
- `db/`(SQLAlchemy·Alembic), `config.py`(pydantic-settings), `routers/`(API 계약 3종) 추가
- 루트 `pyproject.toml`을 **virtual workspace 루트**로 전환, `src/` 제거
- `docker-compose.yml`(FastAPI+PostgreSQL+adminer), `.env.example`, `tests/`, `datasets/golden/` 신설

## Consequences (결과·트레이드오프)

**장점**
- 한 서비스로 로컬 한 번에 기동(`docker compose up`) → 병렬 개발·시연 단순화
- DB/테스트/데이터셋 표준 위치 확보로 착수 블로커 해소

**비용/유의**
- 모듈 경계가 물리 분리가 아닌 디렉토리 수준 → 책임 분리는 코드 규율로 유지
- `uv.lock` 재잠금 필요(멤버 변경 반영)

**Post-MVP 전환 경로 (역전 가능)**
- 대용량 분산 스캔 필요 시 `services/scheduler` → **AWS Step Functions/ECS Fargate**
- 0.5초 실환경 선제 차단 필요 시 `security/soar` → **Lambda/EventBridge**
- 필요 시 `ai/` → 독립 서비스(`packages/ai` 또는 별도 앱)로 분리

## Related

- 현황 기준: [`docs/PROJECT_STATUS.md`](../PROJECT_STATUS.md) — 범위·마일스톤
- 후속 결정 후보: LangGraph 도입 여부, OCSF 스키마·Cedar Policy 채택 → 별도 ADR
