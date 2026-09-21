# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 자원이 맡은 **자리(role)** 를 밝히는 태그의 단일 원천. (Issue #359)
#
# 격리용 SG 는 EC2_ISOLATE 가 발동하기 전까지 어디에도 붙지 않아, 태그가 없으면
# evaluate_sg 가 첫 회차부터 UNUSED(삭제 후보)로 본다. 이름(`*-isolation`)이나 규칙 수로
# 가르는 것은 추정이라 **자원을 만든 쪽이 의도를 태그로 박고, 판정이 그 태그를 읽는다.**
#
# 태그를 다는 쪽(scripts/provision_smoke_aws.py)과 읽는 쪽(services/rule_engine.py)이
# 문자열을 따로 적으면 원천이 둘이 된다 — #342 가 ARN 조립에서 닫은 것과 같은 구멍이다.
# 그래서 두 쪽 모두 여기 상수를 import 한다.
#
# 이 모듈은 자리만 정의한다. 어느 자리를 판정에서 빼는가는 규칙 쪽(rule_engine)이 정한다.
# ==============================================================================

from __future__ import annotations

ROLE_TAG_KEY = "vigilantis:role"
"""자리 태그 키. 키·값 모두 정확일치로 읽는다(대소문자 보정 없음 — 스크립트가 박은 값 그대로)."""

ROLE_ISOLATION = "isolation"
"""격리용 SG. EC2_ISOLATE 의 isolation_group_id 로 쓰이며 평소에는 미부착이 정상이다."""
