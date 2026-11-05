// 고객사 상세의 관제 상태줄에 붙는 범위 표시(PR #578 리뷰). 수집 상태 · 갱신 시각은 서버가 조회 범위 전체로 합쳐 보내
// 그 고객사의 값이 아니다(lib/monitoring-scope.ts). 범위 전체에서 수집하지 못한 유형도 여기에만 적는다. 이 고객사 몫인지
// 알 수 없어 유형 칸에 붙이지 않는다.

import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import type { ScopeWideCollection } from '@/lib/monitoring-scope';

export function ScopeWideNote({ value }: { value: ScopeWideCollection }) {
  return (
    <span>
      수집 상태 · 갱신 시각은 전체 고객사 기준
      {value.uncollected.length > 0 ? (
        <span className="text-badge-warning-fg">
          {' '}
          · 전체 고객사 중 수집하지 못한 유형{' '}
          {value.uncollected.map((u) => `${ASSET_TYPE_LABELS[u.asset_type]?.label ?? u.asset_type}(${u.reason_code})`).join(' · ')}
          , 이 고객사 몫인지는 확인할 수 없습니다
        </span>
      ) : null}
    </span>
  );
}
