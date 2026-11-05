// 지금 적용 중인 구독(투영 · T-16 · #528)을 그리는 공용 조각 — 고객사 계정의 구독 화면과 고객사 관리 목록 · 상세가 같은
// 표기를 쓴다. 조회 전용이다(고객사 관리 화면에 편집 UI 없음).
//
// 훅이 없어 `'use client'`를 붙이지 않는다(panel.tsx와 같은 이유). 서비스 상태와 계약은 따로라 이용 중단이어도
// 구독 칸은 지금 권리 그대로다. 원천(수동 등록 · 모의 계약 · 앱 기본 Free · 미등록)을 함께 보이고, 모의 계약
// 투영의 끝이 기준 시각(`asOf`)에 지났으면 직전 기간의 기록임을 말한다 — 요금제 이름만으로 지금 권리가 있다고
// 그리지 않는다. 계약 패널과 함께 쓰는 요약에서는 정확한 시각 · 계약 설명을 그 패널에 맡긴다.

import { Row } from '@/components/detail-row';
import { EnumBadge } from '@/components/status-badge';
import {
  anchorDayText,
  entitlementLapsed,
  formatAmount,
  LAPSED_NOTE,
  periodText,
  planLabel,
  RENEWAL_OFF,
  sourceLabel,
  sourceNote,
  subscriptionListNote,
} from '@/lib/subscriptions';
import { formatKst } from '@/lib/utils';
import type { IsoDateTime, SubscriptionSource, TenantListItem, TenantSubscription } from '@/types/api';

export function SubscriptionFacts({
  subscription,
  source,
  entitlementEndsAt,
  asOf,
  presentation = 'standalone',
}: {
  subscription: TenantSubscription | null;
  source: SubscriptionSource | null;
  entitlementEndsAt: IsoDateTime | null;
  /** 지난 권리 판정의 기준 시각(ms) — 이 값을 읽은 시각이다. 렌더 중 시계를 읽지 않는다. */
  asOf: number;
  presentation?: 'standalone' | 'summary';
}) {
  if (subscription === null) {
    return (
      <div className="flex flex-col gap-3">
        <Row label="원천"><EnumBadge entry={sourceLabel(null)} /></Row>
        <p className="text-muted-foreground text-xs">구독 정보가 등록되지 않았습니다. Free로 보지 않습니다.</p>
      </div>
    );
  }
  const mock = source === 'MOCK_MARKETPLACE';
  const summary = presentation === 'summary';
  const anchor = anchorDayText(subscription.billing_anchor_day);
  const note = mock && summary ? null : sourceNote(source, subscription);
  return (
    <div className="flex flex-col gap-3">
      <Row label="요금제">
        <span className="inline-flex flex-wrap items-center justify-end gap-1.5 font-semibold">
          {planLabel(subscription)}
          {mock && subscription.cancel_at_period_end ? <EnumBadge entry={RENEWAL_OFF} /> : null}
        </span>
      </Row>
      <Row label="원천"><EnumBadge entry={sourceLabel(source)} /></Row>
      <Row label="요금">{formatAmount(subscription.amount_minor, subscription.currency)}</Row>
      <Row label="이용 기간">
        <span className="tabular-nums">{periodText(subscription)}</span>
      </Row>
      {!summary && mock && entitlementEndsAt !== null ? (
        <Row label="권리 끝 시각"><span className="tabular-nums">{formatKst(entitlementEndsAt, '24h')}</span></Row>
      ) : null}
      {!summary && anchor !== null ? <Row label={mock ? '계약 기념일' : '갱신 기준일(기록)'}>{anchor}</Row> : null}
      {note !== null ? <p className="text-muted-foreground text-xs">{note}</p> : null}
      {mock && entitlementLapsed(entitlementEndsAt, asOf) ? (
        <p role="status" className="bg-badge-warning-bg text-badge-warning-fg rounded-md p-2 text-xs font-semibold">
          {summary ? '직전 기간 기록 · 서버 반영 대기' : LAPSED_NOTE}
        </p>
      ) : null}
    </div>
  );
}

/** 고객사 관리 목록의 구독 칸 — 요금제 한 줄과 원천 · 갱신 · 마지막 이용일 한 줄. */
export function SubscriptionCell({
  item,
}: {
  item: Pick<TenantListItem, 'subscription' | 'subscription_source'>;
}) {
  const subscription = item.subscription;
  const note = subscriptionListNote(item);
  return (
    <span className="flex flex-col gap-0.5">
      <span className={subscription === null ? 'text-muted-foreground text-xs' : 'text-sm'}>
        {planLabel(subscription)}
      </span>
      {note !== null ? <span className="text-muted-foreground text-xs">{note}</span> : null}
    </span>
  );
}
