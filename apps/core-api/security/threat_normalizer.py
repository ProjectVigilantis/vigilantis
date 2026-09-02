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
# 길이는 해시로 묶는다. 원문을 그대로 넣으면 ARN(최대 512) 하나만으로도 컬럼 상한
# 512 에 닿아 잘릴 수 있고, **잘린 키는 서로 다른 위협을 같은 것으로 만든다**(진짜
# 위협이 조용히 사라진다). 원문 값은 target_arn·payload 컬럼에 그대로 남아 있어
# 키에서 되읽을 필요가 없다.
# ==============================================================================

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from schemas.events import (
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
    occurred = event.occurred_at.astimezone(timezone.utc).isoformat()
    if isinstance(event, OpenIpThreatInput):
        return (
            ThreatEventType.OPEN_IP.value,
            event.target_arn,
            event.protocol.strip().lower(),
            _PORT_ANY if event.from_port is None else str(event.from_port),
            _PORT_ANY if event.to_port is None else str(event.to_port),
            event.source_cidr.strip(),
            occurred,
        )
    return (
        ThreatEventType.SSH_BRUTE_FORCE.value,
        event.target_arn,
        event.source_ip.strip(),
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
