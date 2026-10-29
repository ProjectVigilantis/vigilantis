"""업무 기록의 조회 범위. 요청자 신원에서 범위를 만드는 책임은 auth.access에 있다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantScope:
    """None은 명시적 전체 권한, 빈 tuple은 접근 가능한 고객사가 없음을 뜻한다."""

    tenant_ids: tuple[str, ...] | None

    @property
    def is_platform(self) -> bool:
        """기존 저장소 호출의 전체 범위 판정. PLATFORM 계정 여부를 뜻하지 않는다."""
        return self.tenant_ids is None

    def covers(self, tenant_id: str | None) -> bool:
        """소속 없는 기록은 전체 범위에서만 보인다."""
        return self.is_platform or tenant_id in self.tenant_ids


ALL_TENANTS = TenantScope(tenant_ids=None)


@dataclass(frozen=True)
class MonitoredRegions:
    """조회할 고객사별 관제 리전. 소속 없는 기록은 legacy 리전에서만 조회한다."""

    pairs: tuple[tuple[str, str], ...]
    legacy: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.pairs or self.legacy)

    @property
    def regions(self) -> tuple[str, ...]:
        """수집 상태의 기존 리전별 커버리지 판정에 쓰는 합집합."""
        return tuple(dict.fromkeys((*self.legacy, *(region for _, region in self.pairs))))
