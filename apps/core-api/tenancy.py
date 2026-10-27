# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 업무 기록의 테넌트 범위 — 요청자가 볼 수 있는 기록의 경계. (Post-MVP T-4 2단계 C · #533)
#
#   - 계약: packages/schemas/api/tenants.py 머리말 「기존 경로의 테넌트 범위」. 범위 밖은 없는 것이다.
#   - 플랫폼 계정은 전부 본다. 고객사 계정은 자기 테넌트의 기록만 본다 — 테넌트 없는 MVP 기록
#     (tenant_id NULL)은 어느 고객사에도 속하지 않는다(ADR-0012 §4).
#   - 요청자 신원에서 범위를 만드는 것은 auth.access.tenant_scope_of 다. 이 모듈은 인증을 모른다 —
#     Workflow · 저장소 호출부가 신원 대신 범위만 받게 한다.
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantScope:
    """tenant_id 가 None 이면 전 테넌트(플랫폼 계정), 값이 있으면 그 테넌트만."""

    tenant_id: str | None

    @property
    def is_platform(self) -> bool:
        return self.tenant_id is None

    def covers(self, tenant_id: str | None) -> bool:
        """그 테넌트의 기록이 범위 안인가. MVP 기록(None)은 플랫폼 범위에만 든다."""
        return self.is_platform or tenant_id == self.tenant_id


#: 플랫폼 계정 · 서버 내부 호출의 범위.
ALL_TENANTS = TenantScope(tenant_id=None)
