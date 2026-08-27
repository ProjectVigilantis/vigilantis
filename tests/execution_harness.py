# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 실행 계열 테스트 하네스의 데이터·헬퍼입니다. 픽스처 자체는 conftest.py 에 있습니다.
# (Issue #136)
#
# ── 왜 conftest.py 와 나뉘어 있나 ───────────────────────────────────────────
# 픽스처는 conftest.py 에 있어야 하지만, parametrize 인자(P2_LOCAL_FAIL_CASES 등)는
# **수집 시점**에 필요해서 테스트 모듈이 직접 import 해야 한다.
# 그런데 `from conftest import ...` 는 쓸 수 없다 — CI 는 여러 디렉터리를 한 세션으로
# 돌리고(.github/workflows/ci.yml), 그때 `conftest` 라는 최상위 이름은
# apps/core-api/tests/conftest.py 가 먼저 차지한다. `pytest tests` 단독으로는
# 통과하고 CI 전체 호출에서만 ImportError 가 나는 형태라 특히 늦게 발견된다.
# 그래서 이름이 겹치지 않는 이 모듈에 두고, conftest.py 는 여기서 가져다 픽스처로 감싼다.
#
# 여기에는 pytest 픽스처를 두지 않는다. 순수 데이터·순수 함수만 둔다.
# ==============================================================================

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "apps" / "core-api", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from schemas.api.actions import ExecuteActionRequest  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.guardrails import GuardrailValidationContext  # noqa: E402
from schemas.runbook_parameters import (  # noqa: E402
    CANDIDATE_PARAMETER_MODELS,
    build_precheck_parameters,
)
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws.executor import (  # noqa: E402
    BACKUP_INSTANCE_SPEC,
    BACKUP_NACL_RULE_INDEX,
    BACKUP_SG_AND_TG_MAPPING,
    BACKUP_SG_FULL_RULES,
    RUNBOOK_SPECS,
    BackupRecordView,
    parse_arn,
)

# ==============================================================================
# 공통 상수
# ==============================================================================

# 스키마 조립 전용 계정 ID. LocalStack 실물을 만지는 픽스처는 seeded_account_id 로
# 실제 값을 받는다 — 가드레일 ③ ARN Match 가 대조하는 문자열이 이것이기 때문이다.
SAMPLE_ACCOUNT_ID = "123456789012"
SAMPLE_REGION = "ap-northeast-2"

# 상한 128 = ExecuteActionRequest 계약 = action_executions.idempotency_key 컬럼 폭 (#116).
# 계약 자체의 경계값 검증은 packages/schemas/tests/test_api_actions.py:94 가 이미 한다.
# 여기서는 "실행 계열 테스트가 만드는 키가 그 상한을 넘지 않는다"만 보장한다.
IDEMPOTENCY_KEY_MAX = 128

# 백업 레코드가 필요한 런북 — 손으로 적지 않고 명세에서 파생한다. 적어 두면 런북이
# 늘거나 backup_type 이 붙을 때 이 목록만 낡는다. 현재 4종:
# NACL_RESTORE · EC2_UNISOLATE · SG_RECREATE · EC2_REVERT_SIZE.
BACKUP_REQUIRED_RUNBOOKS: frozenset[str] = frozenset(
    runbook_id for runbook_id, spec in RUNBOOK_SPECS.items() if spec.backup_type is not None
)

# 시드 태그. 이름이 아니라 태그로 찾는 이유는 시드 자산 이름이 바뀌어도 조회 자체는
# 살아 있게 하기 위해서다. 이름 대조는 seeded_instance 픽스처가 따로, 크게 실패시킨다.
SEED_TAG_KEY = "vigilantis:seed"
SEED_TAG_VALUE = "true"

SEED_HINT = (
    "LocalStack 시드가 필요하다 — "
    "`docker compose up -d localstack` 후 `uv run python scripts/seed_localstack.py`"
)


def arn_for(
    resource_type: str,
    resource_id: str,
    *,
    service: str = "ec2",
    account_id: str = SAMPLE_ACCOUNT_ID,
    region: str = SAMPLE_REGION,
) -> str:
    """executor.parse_arn 이 받는 형태(arn:aws:<svc>:<region>:<acct>:<type>/<id>)."""
    return f"arn:aws:{service}:{region}:{account_id}:{resource_type}/{resource_id}"


# ==============================================================================
# A. LocalStack 시드 자산
# ==============================================================================


@dataclass(frozen=True)
class SeededInstance:
    name: str
    instance_id: str
    instance_type: str
    security_group_ids: tuple[str, ...]

    def arn(self, account_id: str, region: str) -> str:
        return arn_for("instance", self.instance_id, account_id=account_id, region=region)


def localstack_reachable(endpoint: str) -> bool:
    """헬스체크만 두드린다. boto3 로 재시도(adaptive 5회)를 태우면 미기동일 때 느리다."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/_localstack/health", timeout=2):
            return True
    except Exception:
        return False


def discover_seeded_instances(ec2) -> dict[str, SeededInstance]:
    """시드 태그가 붙은 running/pending 인스턴스를 {Name 태그: SeededInstance} 로."""
    response = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{SEED_TAG_KEY}", "Values": [SEED_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )
    found: dict[str, SeededInstance] = {}
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
            name = tags.get("Name")
            if not name:
                continue
            found[name] = SeededInstance(
                name=name,
                instance_id=instance["InstanceId"],
                instance_type=instance["InstanceType"],
                security_group_ids=tuple(
                    group["GroupId"] for group in instance.get("SecurityGroups", [])
                ),
            )
    return found


# ==============================================================================
# B. Incident → RunbookCandidate → 실행 요청 조립
# ==============================================================================

# 런북별 최소 유효 후보 파라미터(#154 typed 계약). 값이 없는 3종은 빈 dict 다 —
# 격리·SG 삭제·볼륨 삭제는 대상을 target_arn 이 가리키므로 AI 가 정할 값이 없다.
CANDIDATE_PARAMS_BY_RUNBOOK: Mapping[RunbookId, Mapping[str, Any]] = {
    RunbookId.RUNBOOK_EC2_ISOLATE: {},
    RunbookId.RUNBOOK_NACL_ADD_DENY: {
        "rule_number": 100,
        "cidr_block": "203.0.113.5/32",
        "protocol": "-1",
    },
    RunbookId.RUNBOOK_NACL_RESTORE: {"rule_number": 100, "egress": False},
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED: {},
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: {"target_instance_type": "t3.small"},
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: {"min_size": 1, "max_size": 2},
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: {},
}

# target_arn 이 가리켜야 하는 자원 유형별 예시 ID. 유형 자체는 명세에서 읽는다.
_SAMPLE_RESOURCE_ID = {
    "instance": "i-0123456789abcdef0",
    "security-group": "sg-0123456789abcdef0",
    "network-acl": "acl-0123456789abcdef0",
    "volume": "vol-0123456789abcdef0",
}


def default_target_arn(runbook_id: RunbookId, *, account_id: str = SAMPLE_ACCOUNT_ID) -> str:
    """런북이 요구하는 자원 유형에 맞는 예시 ARN."""
    resource_type = RUNBOOK_SPECS[runbook_id.value].resource_type
    return arn_for(resource_type, _SAMPLE_RESOURCE_ID[resource_type], account_id=account_id)


def make_candidate(
    runbook_id: RunbookId = RunbookId.RUNBOOK_EC2_RIGHTSIZING,
    *,
    incident_id: str = "inc-0001",
    target_arn: Optional[str] = None,
    parameters: Optional[Mapping[str, Any]] = None,
    evidence_ids: Optional[list[str]] = None,
    status: CandidateStatus = CandidateStatus.PENDING_VALIDATION,
    candidate_id: Optional[str] = None,
) -> RunbookCandidateData:
    """RunbookCandidateData 하나. parameters 는 런북 계약대로 typed 로 묶인다(#154).

    display_parameters 는 넘기지 않는다 — 서버가 parameters 에서 파생하며 직접
    채우면 스키마가 거절한다(candidates.py _enforce_contract).
    """
    if runbook_id not in CANDIDATE_PARAMETER_MODELS:
        raise ValueError(
            f"{runbook_id.value} 는 AI 후보가 될 수 없다(롤백 3종) — "
            "복구 접수는 후보가 아니라 원본 Execution 을 거친다(#126)"
        )
    return RunbookCandidateData.model_validate(
        {
            "candidate_id": candidate_id or f"cand-{uuid.uuid4().hex[:12]}",
            "incident_id": incident_id,
            "runbook_id": runbook_id.value,
            "target_arn": target_arn or default_target_arn(runbook_id),
            "parameters": dict(
                parameters if parameters is not None else CANDIDATE_PARAMS_BY_RUNBOOK[runbook_id]
            ),
            "evidence_ids": evidence_ids or ["ev-1"],
            "status": status.value,
        }
    )


def make_precheck_parameters(
    candidate: RunbookCandidateData, *, resource_id: Optional[str] = None, **lookups
):
    """후보 → precheck 파라미터 변환(#154). 조회로 채우는 값만 키워드로 받는다.

    resource_id 를 넘기지 않으면 target_arn 에서 꺼낸다. 파서는 executor.parse_arn
    하나만 쓴다 — 둘이 되면 precheck 가 본 자원과 조치가 향하는 자원이 갈릴 수 있다.
    """
    if resource_id is None:
        parsed = parse_arn(candidate.target_arn)
        if parsed is None:
            raise ValueError(f"target_arn 형식 위반 — {candidate.target_arn}")
        resource_id = parsed.resource_id
    return build_precheck_parameters(
        candidate.runbook_id,
        candidate.parameters,
        resource_id=resource_id,
        evidence_ids=candidate.evidence_ids,
        **lookups,
    )


def make_idempotency_key(prefix: str = "qa") -> str:
    """실행 요청용 키. 상한을 넘으면 자르지 않고 즉시 실패시킨다.

    잘라 버리면 서로 다른 두 요청이 같은 키가 되어, 멱등을 검증하려던 테스트가
    오히려 멱등을 가짜로 만든다 (#116).
    """
    key = f"{prefix}-{uuid.uuid4()}"
    if len(key) > IDEMPOTENCY_KEY_MAX:
        raise ValueError(
            f"idempotency_key 상한 {IDEMPOTENCY_KEY_MAX}자 초과({len(key)}자) — "
            f"prefix '{prefix}' 를 줄일 것"
        )
    return key


def make_execute_request(
    *,
    incident_id: str = "inc-0001",
    runbook_id: RunbookId = RunbookId.RUNBOOK_EC2_RIGHTSIZING,
    idempotency_key: Optional[str] = None,
) -> ExecuteActionRequest:
    """POST /api/v1/actions/execute 요청 본문. SSOT 3필드만 싣는다.

    target_arn·실행 파라미터를 받지 않는 것이 계약이다 — 서버가 저장된 Guardrail
    PASS 제안에서 재구성한다(schemas/api/actions.py).
    """
    return ExecuteActionRequest(
        incident_id=incident_id,
        runbook_id=runbook_id,
        idempotency_key=idempotency_key or make_idempotency_key(),
    )


# ==============================================================================
# C. 원복 계열 백업 레코드 (backup_record_id)
# ==============================================================================

# 백업 종류별 최소 유효 payload. 키 이름의 원천은 executor 의 판정 핸들러다
# (_precheck_revert_size · _precheck_sg_recreate · _precheck_unisolate ·
#  _precheck_nacl_restore). 여기서 이름을 새로 짓지 않는다.
_DEFAULT_BACKUP_PAYLOADS: Mapping[str, Mapping[str, Any]] = {
    BACKUP_INSTANCE_SPEC: {"instance_type": "t3.micro"},
    BACKUP_SG_FULL_RULES: {
        "group_name": "vigilantis-restored",
        "description": "restored by rollback",
        "vpc_id": "vpc-0123456789abcdef0",
        "ingress_permissions": [],
        "egress_permissions": [],
    },
    BACKUP_SG_AND_TG_MAPPING: {
        "security_group_ids": ["sg-0123456789abcdef0"],
        "target_group_arn": (
            f"arn:aws:elasticloadbalancing:{SAMPLE_REGION}:{SAMPLE_ACCOUNT_ID}"
            ":targetgroup/vigilantis-tg/0123456789abcdef"
        ),
    },
    BACKUP_NACL_RULE_INDEX: {"rule_number": 100, "egress": False},
}


class FakeBackupLoader:
    """BackupRecordLoader Protocol 의 인메모리 구현.

    원복 계열 4종에만 배선한다. 다른 런북에 넘겨도 precheck 가 쓰지 않아 무해하지만,
    "이 런북은 백업이 필요하다"는 신호가 흐려져 픽스처를 읽는 사람이 잘못된 전제를 갖는다.

    loader 미배선은 precheck 가 유일하게 예외를 던지는 자리다(executor.py:949) —
    그 경로를 검증하려면 이 로더를 아예 넘기지 않으면 된다.

    services/tests/test_precheck_dispatch.py 의 Loader(김세혁)와 역할이 겹쳐 보이지만
    계층이 다르다. 그쪽은 판정 분기를 흘려보내려고 레코드 1건을 그대로 돌려주고,
    이쪽은 payload_match 까지 실제 계약대로 거른다 — 팀 공용 회귀는 "필터를 빼도
    통과하는" 상태를 남기면 안 되기 때문이다.
    """

    def __init__(self, records: Optional[list[BackupRecordView]] = None) -> None:
        self._records: list[BackupRecordView] = list(records or [])

    def add(self, record: BackupRecordView) -> BackupRecordView:
        self._records.append(record)
        return record

    def get(self, backup_record_id: str) -> Optional[BackupRecordView]:
        for record in self._records:
            if record.backup_record_id == backup_record_id:
                return record
        return None

    def latest_for_target(
        self,
        target_arn: str,
        backup_type: str,
        payload_match: Optional[Mapping[str, Any]] = None,
    ) -> Optional[BackupRecordView]:
        # 나중에 넣은 것이 최신 — 실제 구현의 created_at DESC 를 목록 순서로 대신한다
        for record in reversed(self._records):
            if record.target_arn != target_arn or record.backup_type != backup_type:
                continue
            if payload_match and any(
                record.payload.get(key) != value for key, value in payload_match.items()
            ):
                continue
            return record
        return None


def make_backup_record(
    runbook_id: RunbookId,
    target_arn: str,
    *,
    payload: Optional[Mapping[str, Any]] = None,
    backup_record_id: Optional[str] = None,
) -> BackupRecordView:
    """런북에 맞는 백업 레코드 1건. payload 는 기본값 위에 덮어쓴다."""
    spec = RUNBOOK_SPECS[runbook_id.value]
    if spec.backup_type is None:
        raise ValueError(
            f"{runbook_id.value} 는 백업 레코드를 쓰지 않는다 — "
            f"원복 계열은 {sorted(BACKUP_REQUIRED_RUNBOOKS)} 뿐이다"
        )
    merged = {**_DEFAULT_BACKUP_PAYLOADS[spec.backup_type], **(payload or {})}
    return BackupRecordView(
        backup_record_id=backup_record_id or f"bkp-{uuid.uuid4().hex[:12]}",
        target_arn=target_arn,
        backup_type=spec.backup_type,
        payload=merged,
    )


def make_backup_loader(
    runbook_id: RunbookId,
    target_arn: str,
    *,
    payload: Optional[Mapping[str, Any]] = None,
) -> tuple[FakeBackupLoader, BackupRecordView]:
    """원복 계열 런북용 로더 1건 + 그 레코드.

    레코드를 함께 돌려주는 이유는 파라미터에 실을 backup_record_id 가 따로 필요하기
    때문이다(SG_RECREATE 는 자원 ID 없이 이 값만으로 복원 대상을 가리킨다).
    """
    record = make_backup_record(runbook_id, target_arn, payload=payload)
    return FakeBackupLoader([record]), record


# ==============================================================================
# D. ADR-0007 P2 3종 — "로컬 FAIL 은 정상" 전제
# ==============================================================================
#
# elbv2·autoscaling 은 LocalStack Community 에 없다. 그래서 P2 3종은 로컬에서
# precheck 가 항상 PRECHECK_AWS_ERROR 로 FAIL 인데, 이는 버그가 아니라 ADR-0006 §3
# (단일 스위치·코드 분기 금지)을 지킨 결과다. 분기를 넣어 로컬만 통과시키면 실 AWS
# 에서만 드러나는 경로가 생긴다.
#
# 그 실패가 **드러나는 자리는 런북마다 다르다**(PR #121). 후보 경로 하나로 전제를
# 잡으면 UNISOLATE 를 놓친다 — 롤백은 AI 후보가 될 수 없어서(ADR-0004) 애초에 후보
# 경로를 타지 않기 때문이다. 그래서 GuardrailValidationContext 로 나눈다.


@dataclass(frozen=True)
class P2LocalFailCase:
    runbook_id: RunbookId
    context: GuardrailValidationContext
    missing_service: str  # LocalStack Community 에 없는 서비스
    observed: str  # 그 실패가 관측되는 자리

    @property
    def id(self) -> str:
        return f"{self.runbook_id.value}-{self.context.value}"


P2_LOCAL_FAIL_CASES: tuple[P2LocalFailCase, ...] = (
    P2LocalFailCase(
        RunbookId.RUNBOOK_EC2_ISOLATE,
        GuardrailValidationContext.AI_CANDIDATE,
        "elbv2",
        "후보가 EXECUTABLE 이 되지 못한다",
    ),
    P2LocalFailCase(
        RunbookId.RUNBOOK_EC2_ISOLATE,
        GuardrailValidationContext.AUTO_ISOLATION,
        "elbv2",
        "서버가 시작한 자동 격리가 4단계에서 거절된다",
    ),
    P2LocalFailCase(
        RunbookId.RUNBOOK_EC2_UNISOLATE,
        GuardrailValidationContext.ROLLBACK_EXECUTION,
        "elbv2",
        "원클릭 해제 실행이 거절된다(롤백은 AI 후보가 될 수 없다 — ADR-0004)",
    ),
    P2LocalFailCase(
        RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
        GuardrailValidationContext.AI_CANDIDATE,
        "autoscaling",
        "후보가 EXECUTABLE 이 되지 못한다",
    ),
)

P2_LOCAL_FAIL_RUNBOOKS: frozenset[RunbookId] = frozenset(
    case.runbook_id for case in P2_LOCAL_FAIL_CASES
)


def expects_local_precheck_fail(runbook_id: RunbookId, aws_mode: str) -> bool:
    """이 런북의 precheck 가 '정상적으로' FAIL 하는 환경인가.

    환경을 보는 자리는 aws_mode 하나다 — 실 AWS 에서는 같은 런북이 통과해야 하므로
    "P2 니까 FAIL"이라고 굳히면 6~7주차 스모크(ADR-0006 §4)에서 거짓 실패가 난다.
    """
    return aws_mode == "localstack" and runbook_id in P2_LOCAL_FAIL_RUNBOOKS
