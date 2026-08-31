# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# E2E 시연 시나리오 회귀입니다. 원천 명세는 `docs/E2E_DEMO_SCENARIOS.md`이며,
# 그 문서가 스스로를 "이 파일의 명세"라고 규정한다.
#
# ── 두 층으로 나눈다 ────────────────────────────────────────────────────────
# ① **시연 전제 대조 (지금 돈다)** — 설계서가 시연 대본으로 못 박은 값들이 코드·데이터와
#    계속 맞는가. 입력 케이스 ID·런북 짝·계약 제약이 여기 해당한다. 파이프라인이
#    없어도 전부 확인 가능하며, 어긋나면 **대본이 틀린 채로 발표까지 간다.**
# ② **전 구간 흐름 (skip 유지)** — 감지 → Rule Engine → AI CoT → 가드레일 4단계 →
#    원클릭 실행 → 자동 원복. `execute` 본체(Boto3 실행·`get_waiter`·자동 원복)가
#    미구현이라 아직 못 돈다. 무엇을 검증할지는 docstring에 적어 둔다.
#
# ①이 필요한 이유: 설계서는 200줄인데 대응 코드가 주석 한 줄이었다. 그동안 문서의
# 주장(어느 골든 케이스를 쓰는지, 어느 런북이 짝인지)은 **아무것도 검증되지 않았다.**
#
# ── 겹치지 않게 나눈 자리 ───────────────────────────────────────────────────
#   packages/schemas/tests/test_runbooks_registry.py (안성일) — 레지스트리 자체.
#     도메인 매핑 전수·enum 값 목록·롤백 짝 고정.
#   tests/test_golden_dataset.py — 골든 입력↔정답지 대조와 임계값 드리프트.
#   tests/test_guardrails.py — 가드레일 단계 함수의 통과·차단.
#   이 파일 — **설계서가 지목한 특정 케이스와 런북 짝이 그 문서 주장대로인가.**
#     레지스트리가 "무엇이 있는가"를 본다면 이 파일은 "시연이 그중 무엇을 쓰는가"를 본다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pydantic import ValidationError  # noqa: E402
from schemas.api.incidents import (  # noqa: E402
    ExecutionSummaryItem,
    IncidentCategory,
    IncidentResponse,
    ResponseMode,
)
from schemas.runbooks import (  # noqa: E402
    ROLLBACK_RUNBOOK_BY_MAIN_ID,
    ROLLBACK_RUNBOOK_IDS,
    RunbookDomain,
    RunbookId,
    TriggerSource,
    domain_of,
)
from services.rule_engine import _is_prod  # noqa: E402

GOLDEN = ROOT / "datasets" / "golden"

# 설계서가 이름으로 지목한 입력 케이스. 문서가 값까지 인용하고 있어서(§T1 입력·§T2 입력)
# 데이터가 바뀌면 대본의 숫자가 조용히 거짓이 된다.
T1_INPUT = GOLDEN / "finops" / "input" / "asset_inventory_001.json"
T2_INPUT = GOLDEN / "secops" / "input" / "evt_ssh_bruteforce_001.json"
# 설계서가 **쓰지 않기로 한** 케이스. 이유는 아래 테스트에 있다.
T2_REJECTED_INPUT = GOLDEN / "secops" / "input" / "evt_open_ip_001.json"

TARGET_INSTANCE_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _a1() -> dict:
    """T1이 쓰는 A1 — 임계값 바로 아래의 경계 자산."""
    for asset in _load(T1_INPUT)["ec2_instances"]:
        if asset["arn"] == TARGET_INSTANCE_ARN:
            return asset
    pytest.fail(f"A1({TARGET_INSTANCE_ARN})이 {T1_INPUT.name}에 없습니다 — 설계서 §T1 입력")


# ==============================================================================
# ① 시연 전제 대조 — T1 · FinOps
# ==============================================================================


def test_t1_input_case_matches_the_numbers_quoted_in_the_document():
    """설계서 §T1 입력이 인용한 값 전부.

    문서는 `t3.xlarge · cpu_avg 4.9 · dp 336`을 본문에 적고 **"임계값 바로 아래라
    COST_CANDIDATE"** 라고 발표에서 설명하게 돼 있다. 데이터가 바뀌면 그 설명이
    거짓이 되는데, 발표장에서는 아무도 확인할 수 없다.
    """
    a1 = _a1()
    assert a1["instance_type"] == "t3.xlarge"
    assert a1["metric_summary"]["cpu_avg"] == 4.9
    assert a1["metric_summary"]["cpu_datapoints"] == 336


def test_t1_target_is_not_production_so_the_verdict_is_not_absorbed():
    """A1이 운영 자산이면 판정이 SKIP_PROD_PROTECTED로 흡수돼 T1이 성립하지 않는다.

    보호 규칙이 idle 검사보다 우선하기 때문이다(#95 확정 기준). 태그 하나로 시나리오
    전체가 무너지는 자리라 여기서 못 박는다.

    **판정기를 그대로 부른다.** 인식 키는 environment·env·stage·tier 넷이라
    (`services/rule_engine.py`의 PROD_TAG_KEYS), 그중 하나만 옮겨 적으면 나머지 셋으로
    들어온 prod 태그를 놓친다 — 규칙의 부분 복사본이 되는 자리다(PR #220 리뷰).
    """
    assert not _is_prod(_a1()["tags"]), (
        "A1에 prod 계열 태그가 붙으면 T1 전제가 무너진다 — "
        "SKIP_PROD_PROTECTED로 흡수돼 COST_CANDIDATE가 나오지 않는다"
    )


def test_t1_rightsizing_is_a_finops_runbook_paired_with_revert_size():
    """설계서 §T1 8번의 자동 원복 대상이 레지스트리의 짝과 같은가.

    문서가 `RUNBOOK_EC2_REVERT_SIZE`를 손으로 적고 있어, 짝이 바뀌면 대본만 낡는다.
    """
    assert domain_of(RunbookId.RUNBOOK_EC2_RIGHTSIZING.value) is RunbookDomain.FINOPS
    assert (
        ROLLBACK_RUNBOOK_BY_MAIN_ID[RunbookId.RUNBOOK_EC2_RIGHTSIZING.value]
        == RunbookId.RUNBOOK_EC2_REVERT_SIZE.value
    )


def test_t1_auto_rollback_trigger_source_exists_in_the_contract():
    """§T1 8번이 적은 `AUTO_ON_FAILURE`가 실행 축 어휘에 실제로 있는가."""
    assert TriggerSource.AUTO_ON_FAILURE.value == "AUTO_ON_FAILURE"


# ==============================================================================
# ① 시연 전제 대조 — T2 · SecOps
# ==============================================================================


def test_t2_input_case_matches_the_numbers_quoted_in_the_document():
    """설계서 §T2 입력이 인용한 값 전부 (S3)."""
    s3 = _load(T2_INPUT)
    assert s3["event_type"] == "SSH_BRUTE_FORCE"
    assert s3["source_ip"] == "203.0.113.10"
    assert s3["failed_attempt_count"] == 120
    assert s3["window_seconds"] == 300


def test_t2_and_t1_meet_on_the_same_instance():
    """두 트랙이 한 자산에서 만난다 — 발표에서 "이 서버가 아까 그 서버"라고 짚는 자리다."""
    assert _load(T2_INPUT)["target_arn"] == _a1()["arn"]


def test_t2_must_not_use_the_open_ip_case():
    """**설계서가 배제한 케이스**를 T2 입력으로 쓰면 안 된다.

    이 파일의 이전 placeholder는 시나리오를 `0.0.0.0/0 SSH 개방`이라고 적고 있었는데,
    설계서 §T2 입력 선택 근거는 그 케이스를 **명시적으로 배제**한다. 그대로 쓰면
    `RUNBOOK_NACL_ADD_DENY`의 `cidr_block`이 `0.0.0.0/0`이 되어 **서브넷 인바운드가
    전면 차단**된다 — 명세서 `[SecOps-02]`의 "특정 IP/32 단일 주소만 핀셋 지정"
    안전장치와 정면으로 어긋난다.

    S1은 대상도 다르다(security-group ARN). 이 테스트는 두 케이스가 서로 대체될 수
    없다는 것을 데이터로 고정한다.
    """
    s1 = _load(T2_REJECTED_INPUT)
    assert s1["source_cidr"] == "0.0.0.0/0", "배제 근거가 사라졌다면 설계서를 먼저 갱신할 것"
    assert ":security-group/" in s1["target_arn"]

    s3 = _load(T2_INPUT)
    assert "source_cidr" not in s3, "S3는 대역이 아니라 단일 IP를 준다"
    assert "/" not in s3["source_ip"], "핀셋 차단의 근거 — 단일 주소여야 /32로 좁혀진다"


def test_t2_restore_is_a_main_runbook_so_the_release_button_is_not_a_recovery_action():
    """설계서 §「[해제] 버튼이 렌더되는 필드」의 근거를 계약으로 확인한다.

    `RUNBOOK_NACL_RESTORE`는 **본편 7종**이라 `available_recovery_runbook_ids`에
    올 수 없다 — 그 필드는 validator가 롤백 3종만 허용한다(PR #44 확정 계약).
    따라서 [해제] 버튼은 `recommendations`로 렌더된다. 이걸 반대로 구현하면 FE가
    422를 받는데, 원인이 화면이 아니라 계약에 있어 찾기 어렵다.
    """
    assert RunbookId.RUNBOOK_NACL_RESTORE.value not in ROLLBACK_RUNBOOK_IDS

    with pytest.raises(ValidationError):
        ExecutionSummaryItem.model_validate(
            {
                "execution_id": "exec-1",
                "runbook_id": RunbookId.RUNBOOK_NACL_ADD_DENY.value,
                "status": "SUCCESS",
                "available_recovery_runbook_ids": [RunbookId.RUNBOOK_NACL_RESTORE.value],
                "updated_at": "2026-08-27T06:30:00Z",
            }
        )


def test_t2_response_mode_and_trigger_source_are_different_axes():
    """설계서 §「실행 축과 Incident 축은 다르다」 — 3번 단계의 핵심.

    `PRE_MITIGATION_0_5S`는 **양쪽 enum에 같은 문자열로 존재한다.** 그래서 문서를
    읽고 구현하는 사람이 축을 바꿔 적기 쉽고, 그러면 가드레일 ②가 거절해 T2가
    성립하지 않는다. 레지스트리 테스트가 보는 축 쌍(TriggerSource↔ApprovalMode)과
    **다른 쌍**이라 여기서 따로 잡는다.

    T2 3번의 값은 Incident 축이며, 실행 축의 그 값을 갖는 런북은
    `RUNBOOK_EC2_ISOLATE` 하나뿐인데 그 런북은 1차 시연에서 제외된 P2다.
    """
    assert ResponseMode.PRE_MITIGATION_0_5S.value == TriggerSource.PRE_MITIGATION_0_5S.value

    # 두 번째 단언(`is not`)은 걷어냈다 — 서로 다른 Enum 클래스의 멤버는 `is` 로 같아질
    # 수 없어 **항상 참**이었다(PR #220 리뷰). 이빨이 없는 줄을 이빨로 세면 안 된다.
    #
    # 이 축 혼동을 실제로 잡는 곳은 두 자리인데 아직 없다.
    #   ① 런북별 trigger_source 레지스트리 — packages/schemas/runbooks.py 에 없다.
    #      "PRE_MITIGATION_0_5S 를 갖는 런북은 EC2_ISOLATE 하나뿐"의 원천은 코드가
    #      아니라 SSOT §Action Whitelist 표다.
    #   ② 가드레일 ② 의 trigger_source 대조 — packages/schemas/guardrails.py:12 가
    #      "승인 후 RunbookCommand 계약과 함께 추가한다"로 남겨 뒀다.
    # 둘 중 하나가 서면 그때 여기에 진짜 단언을 붙인다.


# ==============================================================================
# ① 시연 전제 대조 — 공통
# ==============================================================================


def test_demo_tracks_avoid_the_p2_runbooks():
    """시연 두 트랙이 쓰는 런북에 P2 3종이 없다.

    P2 3종(`EC2_ISOLATE`·`EC2_UNISOLATE`·`ENABLE_AUTOSCALING`)은 `elbv2`·`autoscaling`이
    LocalStack Community에 없어 **로컬에서 precheck가 항상 FAIL**이다(ADR-0006 §4,
    ADR-0007). 시연 대본이 이 중 하나를 집으면 로컬 리허설이 불가능해진다.
    """
    from execution_harness import P2_LOCAL_FAIL_RUNBOOKS

    demo_runbooks = {
        RunbookId.RUNBOOK_EC2_RIGHTSIZING,   # T1 5번
        RunbookId.RUNBOOK_EC2_REVERT_SIZE,   # T1 8번 (자동 원복)
        RunbookId.RUNBOOK_NACL_ADD_DENY,     # T2 5번
        RunbookId.RUNBOOK_NACL_RESTORE,      # T2 7번 (원클릭 해제)
    }
    assert not (demo_runbooks & P2_LOCAL_FAIL_RUNBOOKS)


def test_finops_incident_carries_no_risk_fields():
    """계약이 강제하는 카테고리별 필드 차이 — 화면에 T1 위험도 배지가 보이면 계약 위반이다.

    위험 대응 축(`initial_risk_level`·`reviewed_risk_level`·`response_mode`)은
    SECOPS에만 있다. 설계서 §카테고리별 필드 차이가 이 사실에 기대고 있다.
    """
    with pytest.raises(ValidationError):
        IncidentResponse.model_validate(
            {
                "incident_id": "inc-qa-finops",
                "subject_arn": TARGET_INSTANCE_ARN,
                "category": IncidentCategory.FINOPS.value,
                "status": "ANALYZING",
                # SECOPS 전용 축이 FINOPS에 남은 상태. 계약이 여기서 거절해야
                # 화면에 T1 위험도 배지가 뜨지 않는다.
                "initial_risk_level": "HIGH",
                "created_at": "2026-08-27T06:30:00Z",
                "updated_at": "2026-08-27T06:30:00Z",
            }
        )


# ==============================================================================
# ② 전 구간 흐름 — 자동 원복까지 서면 skip 해제
# Boto3 실행 경로는 dev 에 들어갔다(#211 / PR #216 — run_rightsizing_execution).
# 남은 것은 Status Check 실패 주입과 자동 원복 둘이다(설계서 §대조 3번).
# ==============================================================================
#
# 아래 2건은 김세혁의 `execute` 본체(Boto3 실행 → `get_waiter` Status Check → 자동
# 원복)가 서면 열린다. 그때 이 파일 위쪽의 전제 테스트가 이미 입력·런북 짝을
# 보증하고 있으므로, 흐름 테스트는 **상태 전이만** 보면 된다.


@pytest.mark.skip(reason="Status Check 실패 주입·자동 원복 미구현 — 설계서 §대조 3번(김세혁)")
def test_t1_idle_ec2_downsize_and_auto_rollback_flow():
    """T1 전 구간 — 설계서 §T1 단계표 1~9번.

    검증할 상태 전이:
      A1 수집 → `COST_CANDIDATE` 판정
      → Incident `ANALYZING` → 추천 `RUNBOOK_EC2_RIGHTSIZING` → `AWAITING_APPROVAL`
      → `POST /actions/execute` **202** → Execution `IN_PROGRESS`
      → Status Check 2/2 실패 → `FAILED`
      → `RUNBOOK_EC2_REVERT_SIZE` (`trigger_source: AUTO_ON_FAILURE`)
        → 원본 Execution `ROLLBACK_INITIATED` → `ROLLED_BACK`

    핵심: **5번 [조치 실행] 이후 사람 입력이 없다.** 8~9번은 전부 시스템이 한다.
    원복 파라미터는 AI도 화면도 아닌 **DB 백업 레코드(`backup_record_id`)** 에서만 온다.
    """


@pytest.mark.skip(reason="Status Check 실패 주입·자동 원복 미구현 — 설계서 §대조 3번(김세혁)")
def test_t2_ssh_bruteforce_block_and_one_click_release_flow():
    """T2 전 구간 — 설계서 §T2 단계표 1~8번.

    **시나리오는 SSH 브루트포스(S3)다.** `0.0.0.0/0` 개방(S1)이 아니다 —
    위 `test_t2_must_not_use_the_open_ip_case` 참조.

    검증할 상태 전이:
      S3 주입 → Incident `SECOPS` 생성
      → `RUNBOOK_NACL_ADD_DENY` (`trigger_source: USER_APPROVAL`,
         `approval_mode: HUMAN_ONLY`, `cidr_block: 203.0.113.10/32`)
        → Execution `SUCCESS`
      → 관제자 [해제] → `RUNBOOK_NACL_RESTORE` (`USER_APPROVAL`) → `SUCCESS`

    핵심: 막는 것도 푸는 것도 **사람이 판단한다**(`HUMAN_ONLY`). 오탐 시 서브넷
    전체가 끊기므로 의도적으로 사람을 넣었다. 차단 대상은 `/32` 단일 주소다.
    """
