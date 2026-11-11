# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 모의 위협 입력(MockThreatEventInput)을 정규화된 위협 이벤트(NormalizedThreatEvent)로
# 바꾸는 정형화 단계입니다. Threat Ingress → **여기** → Risk Evaluator → Incident Intake.
# (Issue #268)
#
# 이 단계가 프로덕션에 없던 동안 정형화는 테스트 헬퍼 2벌로만 존재했고, 골든 SecOps
# 정답 12건은 프로덕션이 공유하지 않는 변환을 검증하고 있었다. 두 벌은 이미 collected_at
# 에서 갈렸고(now vs occurred_at), 둘 다 threat_event_id 를 `te-{event_id}` 로 만들어
# **DB 에 넣을 수 없는 값**이었다(threat_events.threat_event_id 는 PG uuid).
#
# [서버가 정하는 값 셋]
#   - threat_event_id : 서버 발급. models._new_id 와 같은 uuid4 문자열
#   - collected_at    : 우리가 받은 시각. 발생 시각(occurred_at)과 다른 축이다
#   - deduplication_key : 무엇을 "같은 위협"으로 볼지. 아래 규칙 참조
#
# [중복 억제 키]
# threat_events.deduplication_key 에는 UNIQUE 제약이 있다 — 이 값이 곧 중복 억제
# 그 자체다. **같은 관측이 두 번 배달된 것**을 접는 것이 이 키의 일이다.
#
#   OPEN_IP         : 대상 SG + 프로토콜 + 포트 범위 + 출발지 CIDR + 발생 시각
#   SSH_BRUTE_FORCE : 대상 EC2 + 공격 IP + 시도 횟수 + 관측 창 + 발생 시각
#
# IP·CIDR 은 **키에서만 정준 표기로** 넣는다 — IPv6 는 대소문자·축약만 달라도 같은 주소라
# (2001:DB8::1 = 2001:db8::1) 원문으로 넣으면 같은 관측의 재배달이 별개 위협이 된다.
# payload 에는 받은 원문을 그대로 둔다. IPv4 도 예외가 아니다 — 넷마스크·호스트마스크
# 표기(0.0.0.0/0.0.0.0 · 192.0.2.0/255.255.255.0)가 입력 검증을 통과하므로 여기서
# 표기가 접힌다. 입력 단계가 거르는 것은 앞뒤 공백·호스트 비트·잘못된 값이다(PR #374).
#
# 정하면서 버린 두 안과 이유:
#   ① 입력 event_id 를 그대로 쓴다(종전 테스트 헬퍼) — 건마다 유일해 중복이 영원히
#      성립하지 않는다. 생산자가 id 를 안정적으로 재발급한다는 전제도 필요하다.
#   ② 자원 정체만 쓴다(대상 + 공격 IP) — 골든이 이 안을 부순다. 같은 대상·같은 공격
#      IP 가 20분 뒤 1회에서 60회로 올라오는 쌍이 실제로 있고(S3 → S6, LOW → MEDIUM),
#      S2 → S7 도 하루 뒤 같은 쌍이다. 접으면 **위험도가 올라간 재관측이 사라진다.**
#
# 그래서 관측을 이루는 값 전부를 키에 넣는다. 시각은 UTC 로 맞춰 같은 순간의 다른
# 표기(+09:00 과 Z)가 갈리지 않게 한다.
#
# **이어지는 공격을 한 인시던트로 묶을지(시간 창)는 여기서 정하지 않는다.** 그것은
# 저장 계층의 정책이고 Incident Intake(#254) 소관이다. 규칙을 바꿀 자리는
# `_identity_parts` 하나다.
#
# GuardDuty 탐지(#588 C)는 모의 입력이 아니라 수집 주기가 읽은 원문(GetFindings 의 Finding 1건)에서 바로
# 정규화한다(normalize_guardduty_finding). 같은 탐지는 GuardDuty 가 **같은 ID 를 갱신**해 다시 내보내므로
# (횟수 · 최근 관측 시각이 바뀐다) 키는 계정 + 리전 + 탐지 ID 다 — 갱신값을 키에 넣으면 갱신마다 새 위협이
# 된다. 그래서 첫 관측만 남고 뒤의 갱신(심각도가 바뀌어도)은 접힌다(#588 C 「중복」).
#
# 길이는 해시로 묶는다. 원문을 그대로 넣으면 ARN(최대 512) 하나만으로도 컬럼 상한
# 512 에 닿아 잘릴 수 있고, **잘린 키는 서로 다른 위협을 같은 것으로 만든다**(진짜
# 위협이 조용히 사라진다). 원문 값은 target_arn·payload 컬럼에 그대로 남아 있어
# 키에서 되읽을 필요가 없다.
# ==============================================================================

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
from typing import Any

from pydantic import TypeAdapter

from schemas.events import (
    GuardDutyFindingThreatPayload,
    MockThreatEventInput,
    NormalizedThreatEvent,
    OpenIpThreatInput,
    OpenIpThreatPayload,
    SshBruteForceThreatInput,
    SshBruteForceThreatPayload,
    ThreatEventType,
)

_INPUT_ADAPTER: TypeAdapter[Any] = TypeAdapter(MockThreatEventInput)

_PORT_ANY = "*"  # 포트 미지정(전 포트) — None 을 빈 문자열로 두면 자리 구분이 사라진다


def _payload(event) -> OpenIpThreatPayload | SshBruteForceThreatPayload:
    if isinstance(event, OpenIpThreatInput):
        return OpenIpThreatPayload(
            protocol=event.protocol,
            from_port=event.from_port,
            to_port=event.to_port,
            source_cidr=event.source_cidr,
        )
    return SshBruteForceThreatPayload(
        source_ip=event.source_ip,
        failed_attempt_count=event.failed_attempt_count,
        window_seconds=event.window_seconds,
    )


def _identity_parts(event) -> tuple[str, ...]:
    """관측을 이루는 값 — 이 값들이 모두 같으면 같은 관측의 재배달이다.

    규칙을 바꾸려면 이 함수만 고친다(파일 헤더 §중복 억제 키).
    """
    # 시간대 없는 시각은 UTC 로 본다 — astimezone() 은 naive 를 호스트 지역시각으로 읽어
    # KST 머신에서 다른 위협이 같은 키가 되거나(→ UNIQUE 로 진짜 위협이 조용히 사라짐)
    # 같은 관측이 호스트마다 다른 키를 받는다(#269 리뷰: 김세혁). schemas 의 _as_utc 와 같은 결.
    occurred_at = event.occurred_at
    occurred = (
        occurred_at.replace(tzinfo=timezone.utc)
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(timezone.utc)
    ).isoformat()
    if isinstance(event, OpenIpThreatInput):
        return (
            ThreatEventType.OPEN_IP.value,
            event.target_arn,
            event.protocol.strip().lower(),
            _PORT_ANY if event.from_port is None else str(event.from_port),
            _PORT_ANY if event.to_port is None else str(event.to_port),
            str(ip_network(event.source_cidr)),
            occurred,
        )
    return (
        ThreatEventType.SSH_BRUTE_FORCE.value,
        event.target_arn,
        str(ip_address(event.source_ip)),
        str(event.failed_attempt_count),
        str(event.window_seconds),
        occurred,
    )


def _deduplication_key(event) -> str:
    """중복 억제 키. 규칙 변경은 이 함수 하나만 고치면 된다(파일 헤더 §중복 억제 키)."""
    parts = _identity_parts(event)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{parts[0]}|{digest}"


def normalize_threat_event(
    event,
    *,
    collected_at: datetime | None = None,
    threat_event_id: str | None = None,
) -> NormalizedThreatEvent:
    """검증된 모의 위협 입력 → NormalizedThreatEvent.

    collected_at 을 주지 않으면 호출 시각을 쓴다 — 발생 시각(occurred_at)이 아니다.
    둘은 다른 축이고, 늦게 도착한 위협에서 벌어진다. 테스트가 시각을 고정하려면
    이 인자로 주입한다(그 목적의 인자다).
    threat_event_id 도 같은 이유로 주입 가능하며, 기본은 db/models._new_id 와 같은
    uuid4 문자열이다 — threat_events.threat_event_id 가 PG uuid 라 다른 형식은 적재에서
    캐스트 오류가 된다.
    """
    return NormalizedThreatEvent(
        threat_event_id=threat_event_id or str(uuid.uuid4()),
        source_event_id=event.event_id,
        event_type=event.event_type,
        target_arn=event.target_arn,
        occurred_at=event.occurred_at,
        payload=_payload(event),
        deduplication_key=_deduplication_key(event),
        collected_at=collected_at or datetime.now(timezone.utc),
    )


def normalize_mock_input(
    raw: dict,
    *,
    collected_at: datetime | None = None,
    threat_event_id: str | None = None,
) -> NormalizedThreatEvent:
    """원문 dict(골든 JSON 등) → 입력 계약 검증 → 정규화.

    `$schema` 는 편집기용 키라 걷어낸다 — 입력 모델이 extra="forbid" 다. 계약 위반은
    여기서 ValidationError 로 드러난다(종전 테스트 헬퍼는 원문 키를 직접 읽어 검증을
    건너뛰었다).
    """
    payload = {k: v for k, v in raw.items() if k != "$schema"}
    event = _INPUT_ADAPTER.validate_python(payload)
    return normalize_threat_event(
        event, collected_at=collected_at, threat_event_id=threat_event_id
    )


# ---------------------------------------------------------------- GuardDuty 탐지(#588 C)
#: 대상 자원 ARN 을 만들 수 있는 GuardDuty 자원 유형. 그 밖은 탐지 자체의 ARN 을 대상으로 둔다 — IAM 키 ·
#: S3 버킷 ARN 에는 계정이 없어 소속(incident_intake._owning_tenant_id)을 정할 수 없다.
_GUARDDUTY_INSTANCE = "Instance"


def _guardduty_time(value) -> datetime:
    # GetFindings 의 시각은 ISO 8601 문자열이다(예: 2026-11-10T01:02:03.456Z)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("GuardDuty 탐지 시각이 없습니다")
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def guardduty_finding_updated_at(finding: dict) -> datetime:
    """탐지가 마지막으로 갱신된 시각 — 읽기 커서의 축이다(services/guardduty.read_findings)."""
    return _guardduty_time(finding.get("UpdatedAt"))


def _guardduty_target_arn(finding: dict, region: str, account_id: str) -> tuple[str, str]:
    resource = finding.get("Resource") or {}
    resource_type = resource.get("ResourceType") or "Unknown"
    instance_id = (resource.get("InstanceDetails") or {}).get("InstanceId")
    finding_arn = finding.get("Arn") or ""
    partition = finding_arn.split(":")[1] if finding_arn.count(":") >= 5 else "aws"
    if resource_type == _GUARDDUTY_INSTANCE and instance_id:
        return f"arn:{partition}:ec2:{region}:{account_id}:instance/{instance_id}", resource_type
    if not finding_arn:
        raise ValueError("GuardDuty 탐지에 ARN 이 없습니다")
    return finding_arn, resource_type


def normalize_guardduty_finding(
    finding: dict,
    *,
    account_id: str,
    region: str,
    collected_at: datetime | None = None,
    threat_event_id: str | None = None,
) -> NormalizedThreatEvent:
    """GetFindings 의 Finding 1건 → NormalizedThreatEvent. 형식이 어긋나면 ValueError.

    탐지의 계정 · 리전이 읽은 테넌트 · 리전과 다르면 거절한다 — 위임 관리자 계정은 멤버 계정의 탐지도
    돌려주는데(읽기 조건이 계정으로 거르지만) 그 탐지는 이 테넌트의 것이 아니다.
    """
    finding_id = finding.get("Id")
    if not finding_id:
        raise ValueError("GuardDuty 탐지에 Id 가 없습니다")
    if finding.get("AccountId") != account_id:
        raise ValueError("GuardDuty 탐지의 계정이 읽은 테넌트 계정과 다릅니다")
    if finding.get("Region") != region:
        raise ValueError("GuardDuty 탐지의 리전이 읽은 리전과 다릅니다")
    service = finding.get("Service") or {}
    first_seen = _guardduty_time(service.get("EventFirstSeen") or finding.get("CreatedAt"))
    last_seen = _guardduty_time(service.get("EventLastSeen") or finding.get("UpdatedAt"))
    target_arn, resource_type = _guardduty_target_arn(finding, region, account_id)
    digest = hashlib.sha256(
        "|".join((ThreatEventType.GUARDDUTY_FINDING.value, account_id, region, finding_id)).encode("utf-8")
    ).hexdigest()
    return NormalizedThreatEvent(
        threat_event_id=threat_event_id or str(uuid.uuid4()),
        source_event_id=finding_id,
        event_type=ThreatEventType.GUARDDUTY_FINDING,
        target_arn=target_arn,
        occurred_at=first_seen,
        payload=GuardDutyFindingThreatPayload(
            finding_id=finding_id,
            finding_type=finding.get("Type") or "",
            severity=finding.get("Severity"),
            resource_type=resource_type,
            region=region,
            first_seen_at=first_seen,
            last_seen_at=max(first_seen, last_seen),
        ),
        deduplication_key=f"{ThreatEventType.GUARDDUTY_FINDING.value}|{digest}",
        collected_at=collected_at or datetime.now(timezone.utc),
    )
