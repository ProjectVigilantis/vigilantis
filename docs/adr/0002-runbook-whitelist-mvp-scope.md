# ADR-0002: Action Whitelist는 레지스트리 7종으로 확정하고 전부 MVP 범위로 한다

- **Status**: Accepted (2026-08-13 [ADR-0004](0004-rollback-runbook-whitelist-registration.md)로 **범위 확대** — 롤백 3종 정식 등록으로 Whitelist는 **7종 → 10종**. 본 ADR의 판단은 유효하며 대상 목록만 확장됐다 / 2026-08-31 **1차 개정** — 확정본의 소재를 `docs/PROJECT_STATUS.md` §Action Whitelist로 이관. 아래 §개정 이력)
- **Date**: 2026-08-12
- **Deciders**: 김세혁(PM/Infra), 안성일(AI/Guardrail) 공동 확정 사안

## Context (배경)

4단계 가드레일의 Step 2(Action Whitelist)는 AI가 낼 수 있는 조치 범위를 고정하는 규격으로, AI 엔진과 Boto3 제어 모듈 개발의 공통 기준점이다. 초기 범위 서술은 `RUNBOOK_EC2_DOWNSIZE`, `RUNBOOK_IP_BLOCK` 2종을 **예시**로만 제시했고, 확정 목록 작성이 최우선 업무로 지정되어 있었다.

Runbook Master Registry 작성·검증 과정에서 다음을 정리했다:

- 표·상세 명세·Pydantic Literal 간 ID 불일치 통일(표 기준, 예: `RECOMMEND_DOWNSIZE` → `RIGHTSIZING`)
- 기술 오류 수정: NACL rule_number 최대값 32766, `elbv2.deregister_targets` DryRun 미지원(`dry_run_supported: partial`), 격리 SG 규칙 서술 모순
- 관제자 미응답 타임아웃 1분(`TIMEOUT_ISOLATION_1M`) 통일
- 다운사이징 백업을 스펙 JSON(`SAVE_INSTANCE_SPEC_JSON`) 주 방식으로 확정 (EBS 스냅샷은 선택 보조 — 인스턴스 타입 변경은 EBS 데이터를 건드리지 않고, 타입 원복에는 이전 스펙 정보가 필수)

## Decision (결정)

**Runbook 레지스트리 7종 전부를 MVP 범위로 하고, 그 확정본을 `docs/PROJECT_STATUS.md` §Action Whitelist 표로 둔다.**

| 분류 | Runbook ID |
| --- | --- |
| SecOps | `RUNBOOK_EC2_ISOLATE`, `RUNBOOK_NACL_ADD_DENY`, `RUNBOOK_NACL_RESTORE`, `RUNBOOK_SG_DELETE_ISOLATED` |
| FinOps | `RUNBOOK_EC2_RIGHTSIZING`, `RUNBOOK_EC2_ENABLE_AUTOSCALING`, `RUNBOOK_EBS_DELETE_UNATTACHED` |

- 구 예시(`RUNBOOK_EC2_DOWNSIZE`, `RUNBOOK_IP_BLOCK`)는 폐기한다. IP 차단은 SG 인바운드 대신 **NACL Deny 삽입**(`RUNBOOK_NACL_ADD_DENY`) 방식으로 간다.
- 조치 대상 리소스가 EC2·SG를 넘어 NACL·EBS·ASG(Launch Template)·ALB Target Group까지 확장된다. (관제/수집 중심은 여전히 EC2·SG)

## Consequences (결과·트레이드오프)

**장점**
- AI·가드레일·실행 엔진·FE가 공유하는 조치 어휘가 단일 문서로 고정 → 병렬 개발 기준점 확보
- FinOps(즉효 절감 → 구조 전환)와 SecOps(차단 → 원클릭 해제)의 시연 스토리가 런북 단위로 명확

**비용/유의**
- 구현량이 2종 → 7종(+롤백 런북)으로 증가. 9주 일정 대비 위험 → 컷라인(P0/P1/P2) 운용 필요(`docs/PROJECT_STATUS.md` 참고)
- ~~**미해결**: `rollback_runbook_id`가 참조하는 `RUNBOOK_EC2_UNISOLATE`·`RUNBOOK_SG_RECREATE`·`RUNBOOK_EC2_REVERT_SIZE`가 Whitelist에 미등록~~ ✅ **해소**(2026-08-13, [ADR-0004](0004-rollback-runbook-whitelist-registration.md)) — 우회 정책은 기각되고 **3종 정식 등록**으로 결정됐다(`ai_recommendable: false`·백업 레코드 기반 복원·가드레일 실패 시 수동 개입)
- 구 범위 서술(README·코드 스텁 주석)의 "EC2·SG 한정 / 런북 2종"은 구버전이 됨 → 순차 갱신

## Related

- 확정 규격: [`docs/PROJECT_STATUS.md`](../PROJECT_STATUS.md) §Action Whitelist — 표가 확정본이고, 코드 소재는 `packages/schemas/runbooks.py`·`runbook_parameters.py`
- 현황 기준: `docs/PROJECT_STATUS.md`
- 선행 결정: [ADR-0001](0001-mvp-monorepo-structure.md)
- 후속 결정 후보: 롤백 런북 등록 방식, LangGraph 도입 여부

## 개정 이력

- **2026-08-31 (1차 개정)** — **확정본의 소재를 저장소 안으로 이관.** 본 ADR은 확정본을
  로컬 전용 노트(`.gitignore` 대상 Obsidian 볼트)의 런북 명세서로 지목해 왔다. 그 파일은
  팀원과 CI가 열 수 없어, "Whitelist가 무엇인가"를 저장소만 보고는 검증할 수 없었다.
  Whitelist 확정본을 [`docs/PROJECT_STATUS.md`](../PROJECT_STATUS.md) §Action Whitelist
  표로 옮기고, 코드 소재를 그 아래 §코드 소재 표에 명시한다.

  | 항목 | 이관 후 소재 |
  | --- | --- |
  | Whitelist 10종 목록·위험도·승인·AI 추천 가부·등록 롤백 | `docs/PROJECT_STATUS.md` §Action Whitelist (**확정본**) |
  | ID 판정·도메인 분류·롤백 연결 | `packages/schemas/runbooks.py` |
  | 런북별 파라미터 계약 | `packages/schemas/runbook_parameters.py` (#154) |
  | 허용 AWS 작업(`target_api`)·Dry-Run 지원 | [ADR-0007](0007-guardrail-dryrun-executor-precheck-contract.md) §Context·§5 |

  **결정 자체는 바뀌지 않았다** — 7종(→ADR-0004로 10종) 전부 MVP라는 판단과 대상 목록은
  그대로이며, 바뀐 것은 그 목록을 어디서 읽느냐다. 표와 코드가 어긋나면 표가 이긴다.
