'use client';

// DSH-001 헬스 스코어 목록 — 막대 + 임계선, 그리고 **이름에 마우스를 올리면 따라다니는 상세 팝업**.
//
// 카드 본문은 이름과 숫자 하나뿐이다. 그 숫자가 **무엇의 평균인지**(CloudWatch CPU 14일 평균),
// 임계선 어느 쪽인지, 그래서 규칙 엔진이 뭐라 판정했는지는 줄에 다 적을 수 없다 — 적으면 카드가
// 표가 되고, 정작 한눈에 봐야 할 막대 길이 비교가 사라진다. 그래서 **읽을 때만 꺼내는 팝업**에 담는다.
//
// 팝업은 마우스를 따라간다(`position: fixed`). 줄에 붙여 두면 카드 밖으로 나가는 쪽에서 잘리고,
// 목록이 스크롤될 때 자리가 어긋난다. 커서 기준이면 어디서 열어도 보이는 자리에 뜬다.

import { useCallback, useRef, useState } from 'react';

import { StatusBadge } from '@/components/status-badge';
import { NO_VALUE } from '@/lib/enum-labels';
import { cn, formatKst } from '@/lib/utils';
import type { AssetItem } from '@/types/api';

/** 팝업 크기(실측: 260 × 216). 화면 밖으로 나가지 않게 커서 반대편으로 뒤집을 때 쓴다. */
const CARD_W = 260;
const CARD_H = 220;
/** 커서와 팝업 사이 간격 — 0이면 팝업이 커서를 덮어 아래 줄의 hover가 끊긴다. */
const GAP = 14;

export function HealthScoreList({
  scored,
  threshold,
}: {
  /** 점수가 있는 EC2. 정렬은 호출부(`healthSummary`)가 이미 했다 — 낮은 점수가 위다. */
  scored: readonly AssetItem[];
  /** 저활성 임계선(%). 막대의 세로선 위치이자 팝업 문구의 기준이다. */
  threshold: number;
}) {
  const [hovered, setHovered] = useState<AssetItem | null>(null);
  const [pos, setPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const raf = useRef<number | null>(null);

  // mousemove는 초당 수십 번 온다 — 프레임당 한 번만 반영한다. 매번 setState 하면 목록을 훑을 때
  // 리렌더가 쌓여 팝업이 커서를 늦게 따라온다.
  const track = useCallback((event: React.MouseEvent) => {
    const { clientX, clientY } = event;
    if (raf.current !== null) return;
    raf.current = requestAnimationFrame(() => {
      raf.current = null;
      setPos({ x: clientX, y: clientY });
    });
  }, []);

  return (
    <>
      <ul className="flex flex-col gap-3">
        {scored.map((asset) => {
          const score = asset.health_score ?? 0;
          return (
            <li key={asset.arn} className="flex flex-col gap-1">
              {/* 판정 배지는 달지 않는다. 이 카드가 말하는 것은 **점수 하나**이고, 그 판정은
                  바로 옆 `판정 현황`이 전량을 세며 토폴로지 노드도 같은 배지를 단다 —
                  세 곳이 같은 값을 반복하면 정작 여기서 봐야 할 임계선 아래 막대가 묻힌다.
                  대신 팝업이 그 값을 쥔다. */}
              <span className="flex items-center justify-between gap-2 text-xs">
                <span
                  className="min-w-0 truncate underline-offset-2 hover:underline"
                  onMouseEnter={() => setHovered(asset)}
                  onMouseMove={track}
                  onMouseLeave={() => setHovered(null)}
                >
                  {asset.name ?? asset.resource_id}
                </span>
                <span className="font-mono tabular-nums">{score}</span>
              </span>
              <span className="bg-muted relative block h-1.5 overflow-hidden rounded-full">
                <span
                  className={cn(
                    'absolute inset-y-0 left-0 rounded-full',
                    score < threshold ? 'bg-amber-400' : 'bg-emerald-500',
                  )}
                  style={{ width: `${score}%` }}
                />
                {/* 임계선 — 선 왼쪽에서 끝나는 막대가 조치 대상이다(§4.1). */}
                <span
                  aria-hidden
                  className="bg-foreground/70 absolute inset-y-0 w-px"
                  style={{ left: `${threshold}%` }}
                />
              </span>
            </li>
          );
        })}
      </ul>

      {hovered !== null ? <HoverCard asset={hovered} threshold={threshold} at={pos} /> : null}
    </>
  );
}

function HoverCard({
  asset,
  threshold,
  at,
}: {
  asset: AssetItem;
  threshold: number;
  at: { x: number; y: number };
}) {
  const score = asset.health_score;
  const idle = score !== null && score < threshold;
  // 창 가장자리에서는 커서 반대편에 띄운다 — 그러지 않으면 오른쪽·아래쪽 항목의 팝업이 잘린다.
  const left = at.x + GAP + CARD_W > window.innerWidth ? at.x - GAP - CARD_W : at.x + GAP;
  const top = at.y + GAP + CARD_H > window.innerHeight ? at.y - GAP - CARD_H : at.y + GAP;

  return (
    // `pointer-events-none` — 팝업이 커서를 받으면 그 아래 이름의 hover가 끊겨 깜박인다.
    <div
      role="tooltip"
      className="bg-card border-border pointer-events-none fixed z-50 flex w-[260px] flex-col gap-2 rounded-md border p-3 shadow-lg"
      style={{ left, top }}
    >
      <span className="truncate text-xs font-medium">{asset.name ?? asset.resource_id}</span>

      <span className="flex items-baseline gap-1.5">
        <span className="font-mono text-xl tabular-nums">{score ?? NO_VALUE}</span>
        <span className="text-muted-foreground text-[11px]">% · CPU 평균</span>
      </span>

      {/* 숫자만으로는 "높으면 좋은가"를 알 수 없다 — 임계선 어느 쪽인지와 그 뜻을 함께 적는다. */}
      <span className={cn('text-[11px]', idle ? 'text-amber-400' : 'text-muted-foreground')}>
        {score === null
          ? '점수 없음 — 관측치가 없습니다'
          : idle
            ? `임계선 ${threshold}% 미만 — 스펙 조정 후보`
            : `임계선 ${threshold}% 이상 — 사용 중`}
      </span>

      <span className="flex flex-wrap items-center gap-1 border-t pt-2">
        {asset.verdict !== null ? <StatusBadge field="verdict" value={asset.verdict} /> : null}
        {/* `제외`는 사유가 있어야 뜻이 산다 — 활성이라 제외인지, 운영 보호라 제외인지. */}
        {asset.skip_reason_code !== null ? (
          <StatusBadge field="skip_reason_code" value={asset.skip_reason_code} />
        ) : (
          <StatusBadge field="evaluation_status" value={asset.evaluation_status} />
        )}
      </span>

      <dl className="text-muted-foreground grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-[11px]">
        <dt>타입</dt>
        <dd className="truncate font-mono">
          {('instance_type' in asset.spec ? asset.spec.instance_type : null) ?? NO_VALUE}
        </dd>
        <dt>AZ</dt>
        <dd className="truncate font-mono">
          {('availability_zone' in asset.spec ? asset.spec.availability_zone : null) ?? NO_VALUE}
        </dd>
        <dt>상태</dt>
        <dd className="truncate font-mono">{asset.state ?? NO_VALUE}</dd>
        <dt>수집</dt>
        <dd className="truncate">{formatKst(asset.collected_at)}</dd>
      </dl>
    </div>
  );
}
