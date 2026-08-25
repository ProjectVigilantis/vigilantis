// AST-001 명함 카드 — 자산 1건. 화면설계서 v1.5 §4.2 "명함 카드 그리드" 바인딩 표를 따릅니다.

import { Card } from '@/components/ui/card';
import { EnumBadge, StatusBadge } from '@/components/status-badge';
import { ASSET_TYPE_LABELS, NO_VALUE, assetStateEntry } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { AssetItem } from '@/types/api';

/* Row·VerdictArea·HealthArea는 AST-002 상세 패널(asset-detail.tsx)도 그대로 쓴다 —
   같은 필드를 두 화면이 다르게 그리면 목록과 상세가 어긋난다(§8). */

/**
 * 판정 영역. `evaluation_status`가 COMPLETED가 아니면 그 상태를 그대로 보여준다
 * — 판정 전·실패를 verdict 없음(정상)으로 읽히게 두지 않는다(§6.2).
 */
export function VerdictArea({ asset }: { asset: AssetItem }) {
  if (asset.evaluation_status !== 'COMPLETED') {
    return <StatusBadge field="evaluation_status" value={asset.evaluation_status} />;
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      {asset.verdict ? <StatusBadge field="verdict" value={asset.verdict} /> : null}
      {asset.verdict === 'SKIP' && asset.skip_reason_code ? (
        <StatusBadge field="skip_reason_code" value={asset.skip_reason_code} />
      ) : null}
    </span>
  );
}

/**
 * 헬스는 EC2 전용 0~100 정수다(계약). 그 외 6종은 항상 null이므로 게이지를 0으로
 * 그리지 않고 `—` + `확인 불가`로 둔다(§3.3).
 */
export function HealthArea({ score }: { score: number | null }) {
  if (score === null) {
    return (
      <span className="flex items-center gap-1.5">
        <span className="text-muted-foreground tabular-nums">{NO_VALUE}</span>
        <EnumBadge entry={{ label: '확인 불가', tone: 'gray' }} />
      </span>
    );
  }
  return <span className="text-foreground tabular-nums">{score}</span>;
}

export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 text-sm">
      <span className="text-muted-foreground shrink-0">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

export function AssetCard({
  asset,
  incidentCount,
  onSelect,
}: {
  asset: AssetItem;
  incidentCount: number;
  onSelect?: (asset: AssetItem) => void;
}) {
  const state = assetStateEntry(asset);

  return (
    <Card
      // 카드 전체가 AST-002 진입점이다(§4.2 액션). 키보드로도 열려야 한다.
      role="button"
      tabIndex={0}
      // 카드 본문은 배지·수치가 뒤섞여 있어 읽어주면 "EC2 vigilantis-web-01 ap-northeast-2
      // 판정 제외 활성 자산 헬스 78…"처럼 나온다. 무엇을 여는 버튼인지만 남긴다.
      aria-label={`${ASSET_TYPE_LABELS[asset.asset_type]?.label ?? asset.asset_type} ${asset.name ?? asset.resource_id} 상세 열기`}
      onClick={() => onSelect?.(asset)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect?.(asset);
        }
      }}
      className={cn(
        'hover:border-ring focus-visible:ring-ring/50 cursor-pointer gap-3 p-4 transition-colors focus-visible:ring-2 focus-visible:outline-none',
      )}
    >
      <div className="flex items-start gap-2">
        {/* 유형 배지를 맨 앞에 둔다 — 자원 7종의 상태·판정 의미가 서로 달라, 유형을 먼저
            읽지 않으면 나머지 값을 해석할 수 없다(§4.2). */}
        <StatusBadge field="asset_type" value={asset.asset_type} />
        <StatusBadge field="resource_role" value={asset.resource_role} />
      </div>

      <div className="min-w-0">
        <p className="truncate font-medium" title={asset.name ?? asset.resource_id}>
          {asset.name ?? asset.resource_id}
        </p>
        <p className="text-muted-foreground truncate text-xs">
          {asset.region}
          {state ? <> · {state.label}</> : null}
        </p>
      </div>

      <div className="border-border/60 flex flex-col gap-1.5 border-t pt-3">
        {/* NOT_APPLICABLE(NACL·ASG·LT·TG)은 판정 영역 자체를 그리지 않는다(§3.2).
            빈 값으로 남기면 "판정을 못 받은 자산"처럼 읽힌다. */}
        {asset.evaluation_status === 'NOT_APPLICABLE' ? null : (
          <Row label="판정">
            <VerdictArea asset={asset} />
          </Row>
        )}
        <Row label="헬스">
          <HealthArea score={asset.health_score} />
        </Row>
        <Row label="인시던트">
          {incidentCount > 0 ? (
            <span className="text-foreground">{incidentCount}건</span>
          ) : (
            <span className="text-muted-foreground">{NO_VALUE}</span>
          )}
        </Row>
      </div>
    </Card>
  );
}
