// DSH-001 메인 대시보드 본문 — 구성은 PR #299가 채택한 지표 + 카드 구성이고, 값은 전부 계약 필드에서
// 파생한다(lib/dashboard). 서버 컴포넌트다 — 실시간 이벤트가 오면 RealtimeProvider의 `router.refresh()`가
// 이 트리를 다시 그려 숫자가 따라온다.
//
// AI 조치 제안 카드(`action-proposal-card.tsx`)는 페이지가 `proposalSlot`으로 넘기고 이 격자가 자리를 준다.
//
// ## 대시보드는 요약만 남긴다 (2026-09-28)
//
// 종전에는 지표 띠 아래에 집계 3열(자산 인벤토리 · 헬스 스코어 · 판정 현황)과 추이 2축(EC2 CPU ·
// 인터넷 개방 SG), 오른쪽 레일에 개방 SG 목록이 있었다. 전부 **들여다보는 것**이라 축마다 제 화면으로
// 갔다 — 집계 3열·CPU 추이는 자산 관제(AST-001, `assets/asset-summary-panels.tsx`), 개방 SG 목록·추이는
// 보안 관제(SEC-001, `security/security-summary-panels.tsx`). 한 화면 흐름 안에서 같은 수를 두 곳이
// 말하면 관제자가 어느 쪽을 봐야 할지부터 정해야 한다. 여기 남는 것은 **셈(지표 띠 두 묶음) ·
// 구성과 흐름(자산 분류 비율 · 자산 현황 추이 · 위협 판정 추이) · 조치(AI 제안)** 셋이다.
//
// **토폴로지도 제 화면으로 갔다(2026-09-30).** 자산 관제·보안 관제에 각각 토폴로지 탭이 있어, 대시보드
// 아래 전폭 카드는 같은 그래프를 한 번 더 그리는 자리였다. 구조는 들여다보는 것이라 위 규칙을 따른다.
//
// ## 차트 3열 — 지금의 구성 | 자산 수의 흐름 | 위협의 흐름 (2026-09-29)
//
// 띠가 "지금 몇 건"이라면 도넛은 "그 전량이 **무엇으로** 이뤄졌나"(유형별 비율), 자산 현황 추이는
// "**내 자산이 늘었나 줄었나**"(유형별 자산 수 — 시계열 축 5 `asset_inventory`), 위협 판정 추이는
// "위협이 줄고 있나"(축 4 `asset_status`의 `threat`)다. 위협을 자산 수와 한 차트에 두지 않는 이유는
// 눈금이다 — 0–2건이 10여 건과 한 축에 서면 바닥에 붙어 변화가 안 읽힌다. 낭비 후보는 띠의 한 칸으로만
// 둔다: 추이 자리는 자산의 증감을 보는 곳이다(사용자 지시).
//
// ## 지표 띠는 자산 | 보안 두 묶음이다
//
// 5종을 한 줄에 늘어놓던 것을 **관제 축**으로 갈랐다 — GNB가 인시던트를 `보안 인시던트`·`자산 인시던트`로
// 가르는 것과 같은 축이다(gnb.tsx). 그래서 `미조치 인시던트` 한 칸도 카테고리별 두 칸이 됐다
// (FINOPS → 자산, SECOPS → 보안). 두 칸의 합은 종전 한 칸과 같고, AI 조치 제안 카드의 큐와도 같은
// 집합이다(`lib/dashboard` `actionQueue`). 묶음 캡션의 링크는 그 축의 상세 화면으로 간다 — 요약은
// 여기, 들여다보는 것은 거기라는 이 화면의 규칙을 캡션이 그대로 말한다.
//
// ## 배치 — 9칸 | 3칸 두 열 (`xl` 이상)
//
// 왼쪽 9칸은 **보는 것**, 오른쪽 3칸은 **누르는 것**으로 가른다.
//
//   상태줄 (테두리 없는 얇은 캡션 — 격자 밖 맨 위)
//   ┌ 자산 (3) ──────┬ 보안 (3) ──────┬ AI 조치 제안 ────┐
//   │ 자산 분류 비율   │ 자산 현황 추이   │                  │
//   └───────────────┴───────────────┴───────────────┘
//
// **두 열은 윗선과 아랫선을 함께 맞춘다(2026-09-29).** AI 제안 카드는 왼쪽 열(띠 + 차트 두 장) 높이를
// 그대로 받고, 건수가 많으면 카드 안 목록만 스크롤한다 — 카드가 길어져 왼쪽 열을 늘리지 않는다.
//
// **상태줄은 격자 밖이다** — 왼쪽 열 안에 두면 띠가 그 높이만큼 내려앉아 맞춰야 할 윗선 하나가 어긋난다.
//
// `xl` 미만에서는 한 열로 접힌다 — 띠 두 묶음, 차트, AI 제안 순이다.

import Link from 'next/link';

import { AssetCompositionChart } from '@/components/dashboard/asset-composition-chart';
import { InventoryTrendChart, ThreatTrendChart } from '@/components/dashboard/trend-charts';
import { MetricStrip, MetricTile } from '@/components/metric-strip';
import { Panel } from '@/components/panel';
import { Muted } from '@/components/panel-parts';
import { StatusBadge } from '@/components/status-badge';
import { assetComposition, dashboardMetrics } from '@/lib/dashboard';
import { formatKst } from '@/lib/utils';
import type {
  AssetInventoryAxis,
  AssetStatusAxis,
  AssetsResponse,
  IncidentListItem,
} from '@/types/api';

/** 띠 한 묶음 — 캡션(축 이름 + 상세 화면 링크) 아래 타일 3칸. 두 묶음이 같은 모양이어야 나란히 읽힌다. */
function MetricGroup({
  label,
  href,
  hrefLabel,
  children,
}: {
  label: string;
  href: string;
  hrefLabel: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-1.5" aria-label={label}>
      <div className="flex items-center justify-between px-1 text-xs">
        <span className="font-medium">{label}</span>
        <Link href={href} className="text-muted-foreground hover:text-foreground hover:underline">
          {hrefLabel} →
        </Link>
      </div>
      <MetricStrip className="grid-cols-3">{children}</MetricStrip>
    </section>
  );
}

export function DashboardView({
  assets,
  incidents,
  assetStatus,
  assetInventory,
  proposalSlot,
}: {
  assets: AssetsResponse;
  /** 인시던트 조회 실패면 null — 자산 화면과 같은 규칙으로 0건과 구분한다. */
  incidents: IncidentListItem[] | null;
  /**
   * 시계열 축 4(자산 현황). **시계열 조회 자체가 실패면 null** — 축은 왔는데 그 축만 실패한 것
   * (`status: UNAVAILABLE`)과 다르고, 그쪽은 차트가 사유와 함께 그린다. 어느 쪽이든 화면은 뜬다.
   */
  assetStatus: AssetStatusAxis | null;
  /** 시계열 축 5(자산 수). null 규칙은 `assetStatus`와 같다. */
  assetInventory: AssetInventoryAxis | null;
  /**
   * 「AI 조치 제안」 카드 — 페이지가 서버에서 그려 넘긴다(`app/page.tsx`).
   * 이 컴포넌트는 **자리만** 준다. 카드를 여기서 만들면 그 안의 CMN-002(`ErrorState`)가
   * RSC 경계를 넘으며 `ApiError`의 code·requestId를 잃는다.
   */
  proposalSlot: React.ReactNode;
}) {
  const items = assets.items;
  const metrics = dashboardMetrics(items, incidents);
  const unhandledNote = metrics.unhandled === null ? '인시던트 조회 실패' : '분석·승인 대기·조치 중';

  return (
    <div className="flex flex-col gap-4">
      {/* 상태줄 — 격자 밖 맨 위(파일 머리말). collection_status가 READY면 배지를 그리지 않는다(§3.2) —
          자산 화면과 같은 줄이다. */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={assets.collection_status} />
        <span>마지막 수집 {formatKst(assets.last_collected_at)}</span>
        <span>자산 {items.length}건</span>
        <span>{incidents === null ? '인시던트 조회 실패' : `인시던트 ${incidents.length}건`}</span>
      </div>

      <div className="grid gap-4 xl:grid-cols-12">
        {/* 왼쪽 — 보는 것. 띠 두 묶음. 캡션 링크가 각 축의 상세 화면(AST-001 · SEC-001)이다. */}
        <div className="flex flex-col gap-4 xl:col-span-9">
          <div className="grid gap-4 md:grid-cols-2">
            <MetricGroup label="자산" href="/assets" hrefLabel="자산 관제">
              <MetricTile label="전체 자산" value={metrics.total} note="수집된 자산 전량" />
              <MetricTile label="낭비 후보" value={metrics.waste} note="최적화 후보 + 미사용" tone="ok" />
              <MetricTile
                label="미조치 자산 인시던트"
                value={metrics.unhandled === null ? null : metrics.unhandled.FINOPS}
                note={unhandledNote}
                tone="warn"
              />
            </MetricGroup>

            <MetricGroup label="보안" href="/security" hrefLabel="보안 관제">
              <MetricTile
                label="인터넷 개방 보안 그룹"
                value={metrics.openSg}
                note="전체 대역 인바운드 허용"
                tone="warn"
              />
              <MetricTile label="위협 판정 자산" value={metrics.threat} note="규칙 엔진 위협 판정" tone="danger" />
              <MetricTile
                label="미조치 보안 인시던트"
                value={metrics.unhandled === null ? null : metrics.unhandled.SECOPS}
                note={unhandledNote}
                tone="warn"
              />
            </MetricGroup>
          </div>

          {/* 차트 3열(파일 머리말). 추이 하나를 9칸 전폭에 펴면 회차 몇 개짜리 계단이 옆으로 늘어져
              변화가 안 읽힌다. */}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <Panel title="자산 분류 비율" description="수집된 자산의 유형별 구성">
              <AssetCompositionChart composition={assetComposition(items, assets.uncollected)} />
            </Panel>

            {/* 띠의 `전체 자산`을 시간 축으로 편 것이다 — 도넛과 같은 조각·색으로 쌓아 맨 윗선이 전량이다.
                수집 회차가 남기는 유형별 건수가 원천이라 그 기록 전의 회차는 점이 없다(축 5 계약). */}
            <Panel title="자산 현황 추이" description="수집 회차별 유형별 자산 수">
              {assetInventory === null ? (
                <Muted>추이를 불러오지 못했습니다.</Muted>
              ) : (
                <InventoryTrendChart axis={assetInventory} />
              )}
            </Panel>

            <Panel title="위협 판정 추이" description="수집 회차별 위협 판정 자산 건수">
              {assetStatus === null ? (
                <Muted>추이를 불러오지 못했습니다.</Muted>
              ) : (
                <ThreatTrendChart axis={assetStatus} />
              )}
            </Panel>
          </div>
        </div>

        {/* 오른쪽 — 누르는 것. 격자 칸은 왼쪽 열 높이로 늘어나고, 카드는 그 칸에 absolute로 꽉 찬다 —
            카드 내용이 행 높이 계산에 들지 않아 아랫선이 왼쪽 차트 하단에 맞는다(파일 머리말). */}
        {/* 윗변은 띠의 **박스** 윗변에 맞춘다 — 캡션 줄(`MetricGroup`의 text-xs 1rem + gap-1.5)만큼 내린다.
            캡션 글자에 맞추면 카드가 띠 박스보다 한 줄 높이 솟는다. 캡션 모양을 바꾸면 이 값도 함께 고친다. */}
        <div className="flex flex-col xl:col-span-3 xl:pt-[1.375rem]">
          <div className="flex flex-1 flex-col gap-4 xl:relative">{proposalSlot}</div>
        </div>
      </div>
    </div>
  );
}
