# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# ARN 문자열을 **만드는 단일 원천**. (Issue #342)
#
# 이 저장소에서 자산을 잇는 키는 전부 ARN 문자열이고, 가드레일 ③ ARN Match 가 그
# 문자열을 **완전일치**로 대조한다(ai/guardrails.py). 그 문자열을 만드는 자리가 셋으로
# 갈려 있으면(수집기 헬퍼·수집기 f-string·AI 평가 복제본) 한 글자만 어긋나도 조치 대상이
# 조용히 거절되거나 AI 선택지에서 빠진다. 그래서 조립은 여기 build_arn 하나로 모은다 —
# 세 곳 중 하나를 옛 f-string 으로 되돌리면 우회 조립 가드 테스트가 먼저 깨진다.
#
# 읽기(파싱)는 services/aws/executor.py 의 parse_arn 이 단일 원천이다. 다만 그 모듈은
# services 계층이라 schemas·db 계층에서 import 할 수 없어, 리전 한 칸만 필요한 정합
# 검사(find_dangling_arns)용으로 arn_region 을 여기 둔다 — 형식은 parse_arn 과 같다.
# ==============================================================================

from __future__ import annotations

from typing import Optional


def build_arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    """자산 ARN 을 만든다 — arn:aws:ec2:<region>:<account>:<type>/<id>.

    수집기가 만드는 EC2 계열 자원(instance·security-group·network-acl·volume·
    launch-template)의 ARN 이 전부 이 형식이다. 가드레일 ③ 이 이 문자열을 assets.arn
    과 완전일치로 대조하므로 포맷을 여기서 고정한다. ASG·ALB Target Group 은 AWS 가
    돌려주는 arn 을 그대로 쓰고 조립하지 않는다(형식이 달라 이 함수의 대상이 아니다).
    """
    return f"arn:aws:ec2:{region}:{account_id}:{resource_type}/{resource_id}"


def arn_region(arn: str) -> Optional[str]:
    """ARN 의 리전 칸(4번째 필드)을 돌려준다. 형식이 아니면 None.

    정합 검사(assets.region 과 ARN 리전이 같은가)에서만 쓴다. 전체 파싱이 필요하면
    services/aws/executor.py 의 parse_arn 을 쓴다 — 그쪽이 읽기의 단일 원천이다.
    """
    if not isinstance(arn, str):
        return None
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return None
    return parts[3] or None
