# ==============================================================================
# [파일 설명]
# PostgreSQL ORM 모델 정의입니다. 자산/인시던트/조치로그/스펙 스냅샷/스킵 사유를
# 저장합니다. (API 응답 DTO는 packages/schemas의 Pydantic 모델을 사용)
#
# 구현 범위(현재): collector 의 'DB 적재'에 필요한 Asset 만 우선 구현.
#   Incident/ActionLog 는 이후 파트(AI/SOAR)에서 채운다. SpecSnapshot 은 자동원복용.
#   health_score / skip_reason 은 수집 단계에선 비우고 rule_engine 이 채운다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Asset(Base):
    """수집·정형화된 EC2/SG 자산 1건. arn 을 안정 키로 upsert 한다.
    schemas.assets.(Ec2Asset|SecurityGroupAsset) ↔ 이 테이블이 대응된다."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arn: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    asset_type: Mapped[str] = mapped_column(String(16), index=True)  # EC2 | SG
    resource_id: Mapped[str] = mapped_column(String(128), index=True)  # i-xxxx / sg-xxxx
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    account_id: Mapped[str] = mapped_column(String(16), index=True)
    region: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    vpc_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # --- EC2 메트릭 요약 (rule_engine Idle 판정 근거) ---
    cpu_datapoints: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cpu_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cpu_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_in_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_out_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- SG 판정 근거 ---
    attached: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # False=미사용 후보
    open_to_world: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # 전체개방 위협

    # --- rule_engine 이 채우는 자리(수집 단계선 NULL) ---
    verdict: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)  # COST_CANDIDATE/THREAT/UNUSED/SKIP
    health_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # --- 원본/시계열 등 스펙별 세부값 보존(메트릭 시계열, 태그, 개방포트 등) ---
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)

    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
