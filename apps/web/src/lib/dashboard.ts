// DSH-001 대시보드 집계 — 요약 API가 없어 `GET /assets`·`GET /incidents` 응답을 FE가 센다(화면설계서 §4.1).
// 렌더와 분리해 `node --test`로 검증한다. 같은 디렉터리 상대 경로만 쓴다 — `node --test`는 `@/` 별칭을
// 해석하지 못한다(타입 전용 import는 스트리핑돼 사라진다).

import { isJudgedAsset } from './asset-filter.ts';
import { ASSET_TYPE_ORDER, VERDICT_LABELS } from './enum-labels.ts';
import { sortByRisk } from './incident-sort.ts';
import type {
  AssetItem,
  AssetType,
  IncidentListItem,
  IncidentResponse,
  IncidentStatus,
  OpenPortRule,
  UncollectedAssetType,
  Verdict,
} from '@/types/api';

/**
 * 미조치 인시던트 — 흐름이 살아 있고 사람이 아직 결론을 내지 않은 건이다(§4.1).
 * `AWAITING_CLOSURE`는 조치가 끝나 종료 판단만 남았고, `FAILED`는 흐름이 멈춰 조치 대기로 세지 않는다.
 */
export const UNHANDLED_STATUSES = [
  'ANALYZING',
  'AWAITING_APPROVAL',
  'ACTION_IN_PROGRESS',
] as const satisfies readonly IncidentStatus[];

/** 낭비 후보 — Rule이 비용 낭비로 판정한 두 verdict(AST-001 `낭비 후보만`과 같은 집합). */
export const WASTE_VERDICTS = ['COST_CANDIDATE', 'UNUSED'] as const satisfies readonly Verdict[];

/**
 * AI 조치 제안 카드가 쓰는 큐(§4.1). **`미조치 인시던트` 지표와 같은 집합**이라 지표의 숫자와
 * 카드의 대기 건수가 어긋나지 않는다 — 한 화면이 같은 것을 두 숫자로 말하면 어느 쪽도 못 믿는다.
 *
 * 정렬은 INC-001 목록과 공유하는 `sortByRisk` 하나다(`incident-sort.ts`) — 대시보드와 목록이
 * 서로 다른 1순위를 내면 관제자가 어느 화면을 믿을지 알 수 없다.
 *
 * 목록 조회 실패(null)는 **빈 큐가 아니라 null로 돌려준다** — 지표의 `unhandled`와 같은 규칙이다.
 */
export function actionQueue(
  incidents: readonly IncidentListItem[] | null,
): IncidentListItem[] | null {
  if (incidents === null) return null;
  return sortByRisk(
    incidents.filter((i) => (UNHANDLED_STATUSES as readonly IncidentStatus[]).includes(i.status)),
  );
}

/**
 * AI 조치 제안 카드가 그릴 것. **조회 실패와 대기 0건을 가른다** — 목록 조회가 실패했는데
 * `승인을 기다리는 조치 제안이 없습니다`를 띄우면, 같은 화면의 `미조치 인시던트` 지표는 `—`(조회 실패)라고
 * 말하는데 카드만 할 일이 없다고 말한다(PR #351 리뷰 2). 대기 0건 문구는 **조회에 성공한 빈 목록**에만 쓴다.
 */
export type ProposalView =
  | { kind: 'LIST_FAILED' }
  | { kind: 'EMPTY' }
  | { kind: 'TOP_FAILED' }
  | { kind: 'READY'; top: IncidentResponse; next: IncidentListItem[] };

/** `top`은 큐 1순위의 상세 조회 결과다. 큐가 비지 않았는데 null이면 그 조회가 실패한 것이다. */
export function proposalView(
  queue: readonly IncidentListItem[] | null,
  top: IncidentResponse | null,
): ProposalView {
  if (queue === null) return { kind: 'LIST_FAILED' };
  if (queue.length === 0) return { kind: 'EMPTY' };
  if (top === null) return { kind: 'TOP_FAILED' };
  return { kind: 'READY', top, next: queue.slice(1) };
}

/**
 * 헬스 스코어 임계선. 원천은 Rule Engine의 `IDLE_CPU_AVG`(`apps/core-api/services/rule_engine.py`)이고
 * 이 값 미만이 저활성 = 스펙 조정 후보다. 서버 상수를 옮긴 사본이라 원본이 바뀌면 함께 고친다.
 */
export const IDLE_CPU_AVG = 5.0;

export type SgAsset = Extract<AssetItem, { asset_type: 'SG' }>;

export function isOpenSg(asset: AssetItem): asset is SgAsset {
  return asset.asset_type === 'SG' && asset.spec.open_to_world.length > 0;
}

export interface DashboardMetrics {
  total: number;
  openSg: number;
  threat: number;
  /** 인시던트 조회 실패면 null — 0건과 구분한다(0은 "할 일 없음"이라는 관제 정보다). */
  unhandled: number | null;
  waste: number;
}

export function dashboardMetrics(
  items: readonly AssetItem[],
  incidents: readonly IncidentListItem[] | null,
): DashboardMetrics {
  return {
    total: items.length,
    openSg: items.filter(isOpenSg).length,
    threat: items.filter((a) => a.verdict === 'THREAT').length,
    unhandled:
      incidents === null
        ? null
        : incidents.filter((i) => (UNHANDLED_STATUSES as readonly IncidentStatus[]).includes(i.status))
            .length,
    waste: items.filter((a) => WASTE_VERDICTS.some((v) => v === a.verdict)).length,
  };
}

export interface InventoryRow {
  type: AssetType;
  /** 그 유형으로 수집된 자산 전량. */
  count: number;
  /**
   * 그중 **목록(AST-001)에 서는 판정 대상** 수. `NOT_APPLICABLE` 4종(NACL·ASG·시작 템플릿·대상 그룹)은
   * 0이다 — 자산 관제 지표 띠가 "목록에 있는 유형"과 "토폴로지에만 있는 유형"을 가르는 근거다.
   * 이 값이 없으면 그 띠에서 NACL을 눌렀을 때 목록이 비는 이유를 화면이 설명하지 못한다.
   */
  judged: number;
  uncollectedReason: string | null;
}

/**
 * 유형 7종을 **0건까지 전부** 낸다 — 빠지면 "수집이 안 된 것"과 "원래 없는 것"이 구분되지 않는다(§4.1).
 * 순서와 표시명은 표기 사전(§3.2) 하나를 따른다.
 *
 * 대시보드의 「자산 인벤토리」 패널과 자산 관제(AST-001)의 유형 지표 띠가 **이 함수 하나**를 쓴다 —
 * 세는 자리가 둘이면 같은 화면 흐름 안에서 EC2 대수가 갈린다.
 */
export function inventoryCounts(
  items: readonly AssetItem[],
  /** 이번 수집에서 조회를 못 한 유형(`GET /assets` 봉투의 `uncollected`). 없으면 전부 정상 0건이다. */
  uncollected: readonly UncollectedAssetType[] = [],
): InventoryRow[] {
  // 0건 유형을 남기는 이 패널의 목적이 "수집 누락과 구분"인데, 정작 누락 여부를 셈만으로는
  // 알 수 없었다 — 못 가져온 유형은 0이 아니라 **모름**으로 그려야 한다.
  const reasons = new Map(uncollected.map((u) => [u.asset_type, u.reason_code]));
  return ASSET_TYPE_ORDER.map((type) => {
    const ofType = items.filter((a) => a.asset_type === type);
    return {
      type,
      count: ofType.length,
      judged: ofType.filter(isJudgedAsset).length,
      uncollectedReason: reasons.get(type) ?? null,
    };
  });
}

/**
 * 개방 규칙 한 줄. 프로토콜 `-1`(전체 트래픽)은 수집기가 `all`로 정형화하고 `FromPort` 키가 없어
 * 포트가 null로 온다(`apps/core-api/services/collector.py`) — 그대로 끼우면 `all/null`이 찍힌다.
 * 가장 위험한 구성이라 가장 분명하게 적는다(PR #299 리뷰).
 */
export function openPortLabel(rule: OpenPortRule): string {
  const cidr = rule.ipv6 ? '::/0' : '0.0.0.0/0';
  if (rule.protocol === 'all') return `${cidr} 전체 트래픽`;
  const ports =
    rule.from_port === null || rule.to_port === null
      ? '전체 포트'
      : rule.from_port === rule.to_port
        ? String(rule.from_port)
        : `${rule.from_port}-${rule.to_port}`;
  return `${cidr} ${rule.protocol}/${ports}`;
}

export interface ExposureRow {
  sg: SgAsset;
  rules: string[];
  /** 이 SG를 `SECURED_BY`로 가리키는 EC2 수 — 역조인이다(§4.1). */
  affectedEc2: number;
}

export function exposureRows(items: readonly AssetItem[]): ExposureRow[] {
  return items.filter(isOpenSg).map((sg) => ({
    sg,
    rules: sg.spec.open_to_world.map(openPortLabel),
    affectedEc2: items.filter(
      (a) =>
        a.asset_type === 'EC2' &&
        a.relationships.some((r) => r.relation_type === 'SECURED_BY' && r.target_arn === sg.arn),
    ).length,
  }));
}

export interface HealthSummary {
  /** 점수가 있는 EC2, 낮은 순 — 조치 대상이 위로 온다. */
  scored: AssetItem[];
  /**
   * 점수가 없는 **EC2** 수. 계약상 `health_score`는 EC2 전용이라 비EC2의 null은 "확인 불가"가
   * 아니라 해당 없음이다 — 전 자산으로 세면 EC2 7대인 화면에 16이 뜬다(PR #299 리뷰).
   */
  unknown: number;
  ec2Total: number;
}

export function healthSummary(items: readonly AssetItem[]): HealthSummary {
  const ec2 = items.filter((a) => a.asset_type === 'EC2');
  const scored = ec2
    .filter((a) => a.health_score !== null)
    .sort((a, b) => (a.health_score ?? 0) - (b.health_score ?? 0));
  return { scored, unknown: ec2.length - scored.length, ec2Total: ec2.length };
}

export interface VerdictCounts {
  /**
   * 판정 대상 자산 수. NACL·ASG·시작 템플릿·대상 그룹(`NOT_APPLICABLE`)은 뺀다 — 그들의 null
   * verdict는 미판정이 아니라 판정하지 않는 자산이다. 헬스 스코어 분모와 같은 종류의 오류다.
   */
  judged: number;
  byVerdict: { verdict: Verdict; count: number }[];
  /** 판정 대상인데 verdict가 아직 없는 자산(판정 대기·실패). */
  pending: number;
  /** 판정에 이르지 못한 자산 — 데이터 부족 SKIP까지 포함한다. verdict와 축이 달라 따로 센다(§4.1). */
  undecidable: number;
}

export function verdictCounts(items: readonly AssetItem[]): VerdictCounts {
  const judged = items.filter(isJudgedAsset);
  return {
    judged: judged.length,
    byVerdict: (Object.keys(VERDICT_LABELS) as Verdict[]).map((verdict) => ({
      verdict,
      count: judged.filter((a) => a.verdict === verdict).length,
    })),
    pending: judged.filter((a) => a.verdict === null).length,
    undecidable: judged.filter(
      (a) =>
        a.skip_reason_code === 'SKIP_INSUFFICIENT_DATA' ||
        a.evaluation_status === 'PENDING' ||
        a.evaluation_status === 'FAILED',
    ).length,
  };
}
