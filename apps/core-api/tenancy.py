"""업무 기록의 조회 범위. 요청자 신원에서 범위를 만드는 책임은 auth.access에 있다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantScope:
    """플랫폼 범위는 tenant_id=None, 고객사 범위는 해당 테넌트 ID다."""

    tenant_id: str | None

    @property
    def is_platform(self) -> bool:
        return self.tenant_id is None

    def covers(self, tenant_id: str | None) -> bool:
        """소속 없는 기록은 플랫폼 범위에서만 보인다."""
        return self.is_platform or tenant_id == self.tenant_id


ALL_TENANTS = TenantScope(tenant_id=None)
