// enum 표시 배지 — enum-labels 사전만 참조합니다(화면설계서 v1.5 §8: 매핑을 화면마다 복제하지 않는다).

import { Loader2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import {
  ASSET_TYPE_LABELS,
  CATEGORY_LABELS,
  COLLECTION_STATUS_LABELS,
  EVALUATION_STATUS_LABELS,
  EXECUTION_STATUS_LABELS,
  INCIDENT_STATUS_LABELS,
  RESOURCE_ROLE_LABELS,
  RESPONSE_MODE_LABELS,
  RISK_LEVEL_LABELS,
  SKIP_REASON_LABELS,
  VERDICT_LABELS,
  type EnumLabel,
  type LabelTone,
} from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type {
  AssetType,
  CollectionStatus,
  EvaluationStatus,
  ExecutionStatus,
  IncidentCategory,
  IncidentStatus,
  ResourceRole,
  ResponseMode,
  RiskLevel,
  SkipReasonCode,
  Verdict,
} from '@/types/api';

/**
 * 사전의 tone(의미) → 실제 색. 색 정의는 여기 한 곳뿐이다.
 *
 * red만 `--danger` 토큰을 쓴다 — 설계서 §0.3이 "빨강 토큰은 하나"로 못박았고, 토폴로지
 * THREAT 노드·공격 경로 엣지가 같은 값을 참조해야 하기 때문이다. 나머지 tone은 그런
 * 제약이 없어 팔레트 클래스를 그대로 둔다(토큰을 늘리면 정의처만 흩어진다).
 */
const TONE_CLASS: Record<LabelTone, string> = {
  neutral: 'bg-muted text-foreground',
  gray: 'bg-muted text-muted-foreground',
  yellow: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  orange: 'bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300',
  red: 'bg-danger/15 text-danger',
  blue: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  green: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  purple: 'bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-300',
};

/**
 * 사전 항목 하나를 그리는 최소 단위. `null`이면 아무것도 그리지 않는다(3.2의 "—").
 * assetStateLabel처럼 Record로 못 만드는 값도 이걸로 그린다.
 */
export function EnumBadge({
  entry,
  className,
}: {
  entry: EnumLabel | null;
  className?: string;
}) {
  if (entry === null) return null;
  return (
    <Badge variant="outline" className={cn('border-transparent', TONE_CLASS[entry.tone], className)}>
      {entry.spinner ? <Loader2 aria-hidden className="animate-spin" /> : null}
      {entry.glyph ? <span aria-hidden>{entry.glyph}</span> : null}
      {entry.label}
    </Badge>
  );
}

/** field별 값 타입 — 실행 status는 여기 없다(ExecutionStatusBadge 전용). */
interface FieldValue {
  asset_type: AssetType;
  resource_role: ResourceRole;
  collection_status: CollectionStatus;
  evaluation_status: EvaluationStatus;
  verdict: Verdict;
  skip_reason_code: SkipReasonCode;
  category: IncidentCategory;
  incident_status: IncidentStatus;
  risk_level: RiskLevel;
  response_mode: ResponseMode;
}

const FIELD_LABELS: {
  [F in keyof FieldValue]: Record<FieldValue[F], EnumLabel | null>;
} = {
  asset_type: ASSET_TYPE_LABELS,
  resource_role: RESOURCE_ROLE_LABELS,
  collection_status: COLLECTION_STATUS_LABELS,
  evaluation_status: EVALUATION_STATUS_LABELS,
  verdict: VERDICT_LABELS,
  skip_reason_code: SKIP_REASON_LABELS,
  category: CATEGORY_LABELS,
  incident_status: INCIDENT_STATUS_LABELS,
  risk_level: RISK_LEVEL_LABELS,
  response_mode: RESPONSE_MODE_LABELS,
};

type StatusBadgeProps = {
  [F in keyof FieldValue]: { field: F; value: FieldValue[F]; className?: string };
}[keyof FieldValue];

/**
 * 3.2 사전의 필드별 배지. `field`가 판별자라 다른 필드 값을 넘기면 컴파일 에러가 난다.
 * 실행 status는 의미가 달라 이 컴포넌트로 그리지 않는다 — ExecutionStatusBadge를 쓴다(3.2).
 */
export function StatusBadge({ field, value, className }: StatusBadgeProps) {
  const map = FIELD_LABELS[field] as Record<string, EnumLabel | null>;
  return <EnumBadge entry={map[value]} className={className} />;
}

/**
 * 실행 status 6종 전용 배지 — Incident status와 같은 배지를 쓰지 않는다(3.2).
 * 5장 상태 기호(✓ ✕ ⟲ ⚠)를 함께 그려 시각적으로도 구분한다.
 */
export function ExecutionStatusBadge({
  value,
  className,
}: {
  value: ExecutionStatus;
  className?: string;
}) {
  return <EnumBadge entry={EXECUTION_STATUS_LABELS[value]} className={className} />;
}
