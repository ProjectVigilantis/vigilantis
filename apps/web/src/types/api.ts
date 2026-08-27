// API 계약 타입 — packages/schemas/api/(PR #44·#47)를 그대로 미러링한 FE의 유일한 계약 타입 정의 위치입니다.

/** UTC ISO 8601 문자열(예: "2026-08-14T10:05:00Z"). */
export type IsoDateTime = string;

/* ────────────────────────────── errors.py ────────────────────────────── */

/** REST 공통 오류 코드 5종. 괄호는 계약상 HTTP 상태. */
export type ErrorCode =
  | 'INCIDENT_NOT_FOUND' // 404
  | 'IDEMPOTENCY_KEY_CONFLICT' // 409
  | 'PROPOSAL_NOT_EXECUTABLE' // 409
  | 'REQUEST_VALIDATION_FAILED' // 422
  | 'INTERNAL_ERROR'; // 500

export interface ErrorDetail {
  code: ErrorCode;
  message: string;
  request_id: string;
}

/** 오류 봉투 — 최상위 키는 "error" 하나뿐이다. */
export interface ErrorResponse {
  error: ErrorDetail;
}

/* ───────────────────────────── runbooks.py ───────────────────────────── */

/** 확정 Action Whitelist 10종 = 본편 7종(ADR-0002) + 롤백 3종(ADR-0004). */
export const RUNBOOK_IDS = [
  // 본편 7종 — AI 추천 가능
  'RUNBOOK_EC2_ISOLATE',
  'RUNBOOK_NACL_ADD_DENY',
  'RUNBOOK_NACL_RESTORE',
  'RUNBOOK_SG_DELETE_ISOLATED',
  'RUNBOOK_EC2_RIGHTSIZING',
  'RUNBOOK_EC2_ENABLE_AUTOSCALING',
  'RUNBOOK_EBS_DELETE_UNATTACHED',
  // 롤백 3종 — 실행 허용, AI 추천 불가
  'RUNBOOK_EC2_UNISOLATE',
  'RUNBOOK_SG_RECREATE',
  'RUNBOOK_EC2_REVERT_SIZE',
] as const;

export type RunbookId = (typeof RUNBOOK_IDS)[number];

/** 롤백 3종 — recommendations에 올 수 없고 available_recovery_runbook_ids에만 온다. */
export const ROLLBACK_RUNBOOK_IDS = [
  'RUNBOOK_EC2_UNISOLATE',
  'RUNBOOK_SG_RECREATE',
  'RUNBOOK_EC2_REVERT_SIZE',
] as const;

export type RollbackRunbookId = (typeof ROLLBACK_RUNBOOK_IDS)[number];

/** AI 추천 가능 = 본편 7종 (ADR-0004 정책 ②). */
export type AiRecommendableRunbookId = Exclude<RunbookId, RollbackRunbookId>;

/* ────────────────────────────── assets.py ────────────────────────────── */

export const COLLECTION_STATUSES = [
  'NOT_COLLECTED',
  'COLLECTING',
  'READY',
  'PARTIAL',
  'FAILED',
] as const;

export type CollectionStatus = (typeof COLLECTION_STATUSES)[number];

export type AssetType =
  | 'EC2'
  | 'SG'
  | 'NACL'
  | 'EBS'
  | 'AUTO_SCALING_GROUP'
  | 'LAUNCH_TEMPLATE'
  | 'ALB_TARGET_GROUP';

/** PRIMARY = EC2·SG, RUNBOOK_SUPPORT = 나머지 5종. */
export type ResourceRole = 'PRIMARY' | 'RUNBOOK_SUPPORT';

export type EvaluationStatus =
  | 'NOT_APPLICABLE'
  | 'PENDING'
  | 'COMPLETED'
  | 'FAILED';

export type Verdict = 'COST_CANDIDATE' | 'THREAT' | 'UNUSED' | 'SKIP';

export type SkipReasonCode =
  | 'SKIP_INSUFFICIENT_DATA'
  | 'SKIP_PROD_PROTECTED'
  | 'SKIP_LOW_UTIL'
  | 'SKIP_WHITELISTED'
  | 'SKIP_ACTIVE';

export type RelationType =
  | 'SECURED_BY' // EC2 → SG
  | 'ATTACHED_TO' // EC2 → EBS
  | 'MEMBER_OF' // EC2 → Auto Scaling Group
  | 'USES' // ASG → Launch Template
  | 'REGISTERED_IN' // EC2 → ALB Target Group
  | 'PROTECTED_BY'; // EC2 → NACL

export interface OpenPortRule {
  protocol: string;
  from_port: number | null;
  to_port: number | null;
  ipv6: boolean;
}

export interface Ec2Spec {
  instance_type: string | null;
  availability_zone: string | null;
  vpc_id: string | null;
  subnet_id: string | null;
  private_ip: string | null;
}

export interface SgSpec {
  description: string | null;
  vpc_id: string | null;
  attached: boolean;
  open_to_world: OpenPortRule[];
}

export interface NaclSpec {
  vpc_id: string | null;
  is_default: boolean;
  associated_subnet_ids: string[];
}

export interface EbsSpec {
  volume_type: string | null;
  size_gib: number | null;
  availability_zone: string | null;
  encrypted: boolean | null;
  attached_instance_ids: string[];
}

export interface AsgSpec {
  min_size: number;
  max_size: number;
  desired_capacity: number;
  health_check_type: string | null;
}

export interface LaunchTemplateSpec {
  latest_version: number | null;
  default_version: number | null;
}

export interface AlbTargetGroupSpec {
  protocol: string | null;
  port: number | null;
  target_type: string | null;
  health_check_path: string | null;
}

export type AssetSpec =
  | Ec2Spec
  | SgSpec
  | NaclSpec
  | EbsSpec
  | AsgSpec
  | LaunchTemplateSpec
  | AlbTargetGroupSpec;

export interface AssetRelationship {
  relation_type: RelationType;
  target_arn: string;
}

interface AssetItemBase {
  arn: string;
  resource_id: string;
  resource_role: ResourceRole;
  name: string | null;
  account_id: string;
  region: string;
  state: string | null;
  relationships: AssetRelationship[];
  evaluation_status: EvaluationStatus;
  /** EC2 전용 0~100 정수(소수 금지). EC2 외 자산·미완료 판정에서는 항상 null. */
  health_score: number | null;
  verdict: Verdict | null;
  skip_reason_code: SkipReasonCode | null;
  collected_at: IsoDateTime;
}

/** asset_type이 spec 모델을 결정한다(서버가 강제) — 판별 유니온으로 미러링. */
type AssetItemOf<T extends AssetType, S extends AssetSpec> = AssetItemBase & {
  asset_type: T;
  spec: S;
};

export type AssetItem =
  | AssetItemOf<'EC2', Ec2Spec>
  | AssetItemOf<'SG', SgSpec>
  | AssetItemOf<'NACL', NaclSpec>
  | AssetItemOf<'EBS', EbsSpec>
  | AssetItemOf<'AUTO_SCALING_GROUP', AsgSpec>
  | AssetItemOf<'LAUNCH_TEMPLATE', LaunchTemplateSpec>
  | AssetItemOf<'ALB_TARGET_GROUP', AlbTargetGroupSpec>;

export interface AssetsResponse {
  collection_status: CollectionStatus;
  last_collected_at: IsoDateTime | null;
  items: AssetItem[];
}

/* ───────────────────────────── actions.py ───────────────────────────── */

/**
 * 실행 상태 6종 = SSOT 4종 + 복구 최종 결과 2종.
 * ROLLED_BACK·ROLLBACK_FAILED는 원본 Execution에만 기록되고,
 * 롤백 자식 Execution은 IN_PROGRESS → SUCCESS | FAILED만 쓴다.
 * FAILED(AWS 변경 없음)와 ROLLBACK_FAILED(AWS 변경된 채 복구 실패·CRITICAL)를 UI에서 합치지 말 것.
 */
export type ExecutionStatus =
  | 'IN_PROGRESS'
  | 'SUCCESS'
  | 'FAILED'
  | 'ROLLBACK_INITIATED'
  | 'ROLLED_BACK'
  | 'ROLLBACK_FAILED';

/** 요청은 3필드만 — Target ARN·AWS 파라미터는 보내지 않는다(extra=forbid). */
export interface ExecuteActionRequest {
  incident_id: string;
  runbook_id: RunbookId;
  idempotency_key: string;
}

/** 202 Accepted(신규 예약) / 200 OK(같은 Key 재요청) 공통 응답 본문. */
export interface ExecuteActionResponse {
  execution_id: string;
  status: ExecutionStatus;
  updated_at: IsoDateTime;
}

/* ──────────────────────────── incidents.py ──────────────────────────── */

export const INCIDENT_CATEGORIES = ['FINOPS', 'SECOPS'] as const;
export type IncidentCategory = (typeof INCIDENT_CATEGORIES)[number];

export const INCIDENT_STATUSES = [
  'ANALYZING',
  'AWAITING_APPROVAL',
  'ACTION_IN_PROGRESS',
  'RESOLVED',
  'FAILED',
] as const;
export type IncidentStatus = (typeof INCIDENT_STATUSES)[number];

export const RISK_LEVELS = ['HIGH', 'MEDIUM', 'LOW'] as const;
export type RiskLevel = (typeof RISK_LEVELS)[number];

/** SSOT 3단계 위험 대응 — 실제 적용된 현재 대응 경로. */
export const RESPONSE_MODES = [
  'PRE_MITIGATION_0_5S',
  'AGENT_WAIT',
  'TIMEOUT_ISOLATION_1M',
] as const;

export type ResponseMode = (typeof RESPONSE_MODES)[number];

export interface RecommendationItem {
  runbook_id: AiRecommendableRunbookId;
  target_arn: string;
  /** 화면 표시 전용 — 실행 요청에 되돌려 보내지 않는다. */
  display_parameters: Record<string, string>;
}

export interface ExecutionSummaryItem {
  execution_id: string;
  runbook_id: RunbookId;
  status: ExecutionStatus;
  /** 관제자 복구 조치 — 롤백 3종만 온다. */
  available_recovery_runbook_ids: RollbackRunbookId[];
  updated_at: IsoDateTime;
}

/** 목록 항목 — 상세(IncidentResponse)의 부분집합 10필드. */
export interface IncidentListItem {
  incident_id: string;
  /** nullable·빈 문자열 금지. null이면 category 표시명 + ARN 축약으로 fallback. */
  title: string | null;
  subject_arn: string;
  category: IncidentCategory;
  status: IncidentStatus;
  /** FINOPS는 위험도 2필드·response_mode 전부 null. */
  initial_risk_level: RiskLevel | null;
  reviewed_risk_level: RiskLevel | null;
  response_mode: ResponseMode | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface IncidentResponse extends IncidentListItem {
  /** 분석 완료 시 정확히 3개, 분석 중·실패 시 빈 배열. */
  summary_lines: string[];
  evidence_ids: string[];
  recommendations: RecommendationItem[];
  executions: ExecutionSummaryItem[];
}

/** 목록 봉투 — 페이지네이션 필드는 Post-MVP. */
export interface IncidentsResponse {
  items: IncidentListItem[];
}

/* ─────────────────────────────── ws.py ─────────────────────────────── */
/* WS 이벤트 봉투. mock에는 엔드포인트가 없고(2026-08-14 확정) 실 BE에만 연결한다(#168). */

export type WsEventType =
  | 'INCIDENT_CREATED'
  | 'INCIDENT_UPDATED'
  | 'EXECUTION_UPDATED';

export interface IncidentEventData {
  incident_id: string;
}

export interface ExecutionEventData {
  incident_id: string;
  execution_id: string;
  status: ExecutionStatus;
  updated_at: IsoDateTime;
}

/** 같은 event_id 재수신은 무시(수신 측 멱등 키). 재연결 시 replay 없음 — REST 재조회로 복구. */
export type WsEvent =
  | {
      event_id: string;
      event_type: 'INCIDENT_CREATED' | 'INCIDENT_UPDATED';
      occurred_at: IsoDateTime;
      data: IncidentEventData;
    }
  | {
      event_id: string;
      event_type: 'EXECUTION_UPDATED';
      occurred_at: IsoDateTime;
      data: ExecutionEventData;
    };
