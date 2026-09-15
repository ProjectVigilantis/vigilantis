# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine) — 하한 규칙 소유 (#251)
# RUNBOOK_EC2_RIGHTSIZING의 목표 인스턴스 타입을 서버가 계산하는 규칙입니다. (Issue #251)
#
# 목표 타입은 AI가 고르지 않는다. #237 계측에서 모델·파라미터 14종 조합 전부에서 흔들린
# 필드가 이것 하나였고, 원인은 모델이 아니라 서버가 값의 범위를 주지 않은 데 있었다.
# 방침이 "최대한 절약"이면 답이 하나로 정해지므로 모델이 고를 이유가 없다 — runbook_id·
# target_arn을 그래프가 고정하는 것과 같은 자리다(ai/agent.py 계약 원칙).
#
# 규칙 — #251 결정(2026-09-14, 김승철 안 채택). 셋을 모두 지키는 가장 작은 크기를 고른다.
#   ① 같은 패밀리 안에서만 내린다. 패밀리를 넘으려면 architecture·메모리 속성이 필요한데
#      그 수집을 하지 않는다(#237 §범위 밖 "자산 속성 수집 확장").
#   ② 현재 메모리의 1/4 미만으로 내리지 않는다. 판정(services/rule_engine.py)은 CPU만 보고
#      메모리는 수집하지 않는다. 저활성 CPU이면서 메모리를 크게 쓰는 자산(JVM 힙·인메모리
#      캐시)도 후보가 되는데, OOM으로 앱만 죽은 상태는 2/2 Status Check를 통과해 자동
#      원복이 걸리지 않는다(services/aws/rollback.py 성공 판정 경계).
#   ③ 메모리 2 GiB(버스트 패밀리의 small) 미만으로 내리지 않는다 — micro·nano는 버스트
#      크레딧 여유가 없다. 크기 이름이 아니라 메모리로 적는 것은 m5처럼 small이 없는
#      패밀리에도 같은 뜻으로 걸기 위해서다.
# 셋을 지키는 크기가 없으면 None이다 — 다운사이징을 제안하지 않는다(ai/capabilities.py가
# 메뉴에서 뺀다). 예: t3.xlarge→t3.medium · t3.large→t3.small · t3.small 이하→None ·
# m5.2xlarge→m5.large · m5.large→None.
#
# 표는 저장소가 다루는 패밀리만 담는다 — 골든·시연의 t3, LocalStack 시드의 m5
# (scripts/seed_localstack.py idle-dev), 크기별 메모리가 t3와 같은 버스트 패밀리 셋.
# 표 밖 패밀리도 None이다. 값은 AWS 인스턴스 유형 공개 사양이며 저장소가 측정한 값이
# 아니다. 패밀리를 넓힐 때는 표에 사양을 더한다 — 추측으로 채우지 않는다.
#
# 비율과 하한은 판정 임계치(IDLE_CPU_AVG·SPIKE_CPU_MAX)와 함께 움직인다. 두 임계치는 아직
# 실 워크로드로 보정되지 않았다(services/rule_engine.py 헤더) — 실 AWS 스모크에서 보정한
# 뒤 비율을 1/8로 푸는 것이 #251에 적힌 다음 단계다.
# ==============================================================================

from __future__ import annotations

from typing import Mapping, Optional

# 크기 → 메모리(MiB). 작은 크기부터 적는다 — 이 순서로 훑어 조건을 지키는 가장 작은 답을 고른다.
_BURSTABLE_MEMORY_MIB: Mapping[str, int] = {
    "nano": 512,
    "micro": 1024,
    "small": 2048,
    "medium": 4096,
    "large": 8192,
    "xlarge": 16384,
    "2xlarge": 32768,
}

# m5는 vCPU당 4 GiB이고 large가 가장 작다
_M5_MEMORY_MIB: Mapping[str, int] = {
    "large": 8192,
    "xlarge": 16384,
    "2xlarge": 32768,
    "4xlarge": 65536,
    "8xlarge": 131072,
    "12xlarge": 196608,
    "16xlarge": 262144,
    "24xlarge": 393216,
}

FAMILY_MEMORY_MIB: Mapping[str, Mapping[str, int]] = {
    "t2": _BURSTABLE_MEMORY_MIB,
    "t3": _BURSTABLE_MEMORY_MIB,
    "t3a": _BURSTABLE_MEMORY_MIB,
    "t4g": _BURSTABLE_MEMORY_MIB,
    "m5": _M5_MEMORY_MIB,
}

FLOOR_MEMORY_MIB = 2048  # ③
MEMORY_RATIO_LIMIT = 4  # ② 현재 메모리의 1/N 미만으로 내리지 않는다

# 사양을 아는 타입 전체. 후보 계약이 이 밖의 목표 타입을 거절한다(runbook_parameters.py).
KNOWN_INSTANCE_TYPES: frozenset[str] = frozenset(
    f"{family}.{size}" for family, sizes in FAMILY_MEMORY_MIB.items() for size in sizes
)


def rightsizing_target_type(current_instance_type: Optional[str]) -> Optional[str]:
    """현재 타입 → 다운사이징 목표 타입. 규칙 셋을 지키며 내릴 수 없으면 None이다."""
    if current_instance_type not in KNOWN_INSTANCE_TYPES:
        return None
    family, size = current_instance_type.split(".", 1)
    sizes = FAMILY_MEMORY_MIB[family]
    current = sizes[size]
    floor = max(FLOOR_MEMORY_MIB, current / MEMORY_RATIO_LIMIT)
    for candidate, memory in sizes.items():
        if floor <= memory < current:
            return f"{family}.{candidate}"
    return None
