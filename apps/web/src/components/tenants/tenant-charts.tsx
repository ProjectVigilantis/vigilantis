// 고객 연결 상태 그래프. GET /tenants 응답 하나로 그린다(시계열은 계약에 없다).
// 형태 · 색 규칙은 DESIGN.md §데이터 시각화를 따른다: 분류끼리 건수 비교라 가로 막대, 막대는 파랑 한 계열,
// 상태 색은 분류 이름 옆 점에만 쓴다.
//
// 고객이 수백 곳이어도 높이가 그대로다(분류 다섯 줄 · 2026-10-29 개편 — 종전 고객마다 한 줄 막대는 고객 수만큼 길어졌다).
// 어느 고객사인지는 고객사 목록에서 본다(대시보드는 숫자와 그래프만 둔다).

import { BarRows, MoreLink } from '@/components/admin-dashboard/chart-parts';
import { ChartCard } from '@/components/chart-card';
import { CONNECTION_STATE_ORDER, connectionPoints, type ConnectionState } from '@/lib/admin-dashboard';
import { TENANTS_PATH } from '@/lib/home-path';
import { VERIFICATION_STALE_HOURS } from '@/lib/tenants';
import type { TenantListItem } from '@/types/api';

const STATE: Record<ConnectionState, { label: string; dot: string; hint: string }> = {
  FAILED: { label: '연결 실패', dot: 'bg-status-critical', hint: '역할 · 권한 문제로 확인 실패' },
  UNREACHABLE: { label: '확인 불가', dot: 'bg-status-idle', hint: '응답 없음, 다시 시도하면 풀릴 수 있음' },
  STALE: { label: '오래됨', dot: 'bg-status-warning', hint: `마지막 정상 확인 후 ${VERIFICATION_STALE_HOURS}시간 넘음` },
  OK: { label: '정상', dot: 'bg-status-good', hint: `${VERIFICATION_STALE_HOURS}시간 안에 정상 확인` },
  // 야간 바탕에서 회색 점이 묻혀 테두리를 둔다.
  UNCHECKED: { label: '확인 전', dot: 'bg-heat-none ring-1 ring-foreground/40', hint: '연결 확인 기록 없음' },
};

export function ConnectionStatusChart({
  tenants,
  nowMs,
  className,
}: {
  tenants: readonly TenantListItem[];
  nowMs: number;
  className?: string;
}) {
  const points = connectionPoints(tenants, nowMs);
  const rows = CONNECTION_STATE_ORDER.map((s) => {
    const n = points.filter((p) => p.state === s).length;
    return {
      key: s,
      label: STATE[s].label,
      value: n,
      dot: STATE[s].dot,
      title: `${STATE[s].label} ${n}곳 (${Math.round((n / Math.max(1, points.length)) * 100)}%) · ${STATE[s].hint}`,
    };
  });

  return (
    <ChartCard
      className={className}
      title="고객사 연결 상태"
      description="정지를 뺀 고객사 · 상태별 고객사 수"
      aside={<span className="text-sm font-bold tabular-nums">{points.length}곳</span>}
    >
      {points.length === 0 ? (
        <p className="text-muted-foreground text-sm">연결 상태를 볼 고객사가 없습니다.</p>
      ) : (
        <BarRows rows={rows} />
      )}
      <MoreLink href={TENANTS_PATH}>고객사 목록</MoreLink>
    </ChartCard>
  );
}
