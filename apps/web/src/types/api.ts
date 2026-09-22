// API 계약 타입 — packages/schemas/api/(PR #44·#47)를 그대로 미러링한 FE의 유일한 계약 타입 정의 위치입니다.

/** UTC ISO 8601 문자열(예: "2026-08-14T10:05:00Z"). */
export type IsoDateTime = string;

/* ────────────────────────────── errors.py ────────────────────────────── */

/** REST 공통 오류 코드 5종. 괄호는 계약상 HTTP 상태. */
export type ErrorCode =
  | 'INCIDENT_NOT_FOUND' // 404
  | 'IDEMPOTENCY_KEY_CONFLICT' // 409
  | 'PROPOSAL_NOT_EXECUTABLE' // 409
  | 'INCIDENT_NOT_RESOLVABLE' // 409
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
  | 'SKIP_ACTIVE'
  | 'SKIP_UNSUPPORTED_STATE';

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

/**
 * 이번 수집에서 **조회 자체를 못 한** 자산 유형. `items`에 그 유형이 0건인 것만으로는
 * "원래 없다"와 "못 가져왔다"가 구분되지 않아 계약이 따로 싣는다.
 */
export interface UncollectedAssetType {
  asset_type: AssetType;
  /** AWS 오류 코드(`AccessDenied`·`InternalFailure` 등) 또는 예외 클래스명. 번역하지 않고 원문 노출. */
  reason_code: string;
}

export interface AssetsResponse {
  collection_status: CollectionStatus;
  last_collected_at: IsoDateTime | null;
  items: AssetItem[];
  /**
   * **비어 있다고 전부 성공했다는 뜻은 아니다** — 서버가 원인을 유형으로 환원하지 못하면
   * 지어내는 대신 비워 둔다. 그 경우에도 `collection_status`는 PARTIAL·FAILED로 남는다.
   */
  uncollected: UncollectedAssetType[];
}

/* ───────────────────────────── actions.py ───────────────────────────── */

/**
 * 실행 상태 7종 = SSOT 4종 + 복구 최종 결과 2종 + 결과 확인 불가 1종(#249).
 * ROLLED_BACK·ROLLBACK_FAILED·UNVERIFIED는 원본 Execution에만 기록되고,
 * 롤백 자식 Execution은 IN_PROGRESS → SUCCESS | FAILED만 쓴다.
 * FAILED(AWS 변경 없음)와 ROLLBACK_FAILED(AWS 변경된 채 복구 실패·CRITICAL)를 UI에서 합치지 말 것.
 */
export type ExecutionStatus =
  | 'IN_PROGRESS'
  | 'SUCCESS'
  | 'FAILED'
  | 'ROLLBACK_INITIATED'
  | 'ROLLED_BACK'
  | 'ROLLBACK_FAILED'
  /**
   * 결과 확인 불가 — AWS 조회 실패로 종료 판정을 내리지 못한 채 재시도를 소진했다(#249).
   * 자동 원복하지 않았고 관제자 확인이 남았다. 종료 상태이며 관제자 복구가 열린다.
   */
  | 'UNVERIFIED';

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
  /** 조치가 끝났고 관제자 종료 판단만 남음 — 남은 제안·진행 중 실행 없음 (#240). */
  'AWAITING_CLOSURE',
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

/**
 * 절감 예상의 결과 상태. **`ESTIMATED`만 금액이 있다** — `UNAVAILABLE`·`INVALID`는
 * `amount`가 null이고, 화면은 그 둘을 0원으로 그리지 않는다(0원은 "절감이 없다"는 단언이다).
 */
export type SavingsStatus = 'ESTIMATED' | 'UNAVAILABLE' | 'INVALID';

/**
 * 미산출 사유. `MODEL_UNAVAILABLE`은 모델이 못 낸 것이고 나머지 셋은 서버가 계약 위반으로
 * 버린 것이다 — 화면은 둘을 같은 말로 덮지 않는다.
 */
export type SavingsReason =
  | 'MODEL_UNAVAILABLE'
  | 'MISSING_ESTIMATE'
  | 'INVALID_ESTIMATE'
  | 'CONTEXT_MISMATCH';

/**
 * 비교 가정. 서버가 **고정 리터럴**로 싣는다 — 730시간·Linux·공유 테넌시·온디맨드의
 * 인스턴스 컴퓨팅 비용만 비교한다는 뜻이고, `pricing_source`가 `MODEL_KNOWLEDGE`인 것은
 * 단가가 **AI 추정**이라는 표시다(AWS Cost Explorer·Price List가 아니다).
 */
export interface SavingsAssumptions {
  hours: number;
  operating_system: string;
  tenancy: string;
  purchase_option: string;
  included_cost: string;
  pricing_source: 'MODEL_KNOWLEDGE';
}

/** 추정 근거. `explanation`은 서버가 쓴 문장이다(`explanation_source = SERVER_TEMPLATE`). */
export interface SavingsBasis {
  target_arn: string;
  region: string;
  current_instance_type: string;
  target_instance_type: string;
  /** USD/시간. 계약이 소수점 6자리 **문자열**로 직렬화한다 — 부동소수 오차를 옮기지 않으려고. */
  current_hourly_rate: string;
  target_hourly_rate: string;
  assumptions: SavingsAssumptions;
  explanation: string;
  explanation_source: 'SERVER_TEMPLATE';
}

/**
 * RIGHTSIZING 조치 1건의 **AI 참고 추정**. 실제 청구액이 아니다(#347) — 화면은 금액 옆에
 * 그 사실을 반드시 함께 적는다. AWS Cost Explorer 연동이 아니라 모델 추정 단가 × 730시간이다.
 */
export interface AiSavingsEstimate {
  status: SavingsStatus;
  currency: 'USD';
  period: 'MONTH';
  /** 월 절감 예상액. `ESTIMATED`가 아니면 null이다. 계약이 소수점 2자리 **문자열**로 싣는다. */
  amount: string | null;
  basis: SavingsBasis | null;
  /** 미산출 사유. `ESTIMATED`면 null이다. */
  reason: SavingsReason | null;
}

export interface RecommendationItem {
  runbook_id: AiRecommendableRunbookId;
  target_arn: string;
  /** 화면 표시 전용 — 실행 요청에 되돌려 보내지 않는다. */
  display_parameters: Record<string, string>;
  /** RIGHTSIZING 조치에만 실린다. 그 밖의 런북에서는 null이다. */
  ai_savings_estimate: AiSavingsEstimate | null;
}

/**
 * 판정 불가 보류 기록(#249) — AWS에 물어보지 못해 실행 결과를 확정하지 못한 이력.
 * 실행 `status`가 IN_PROGRESS면 재시도 중, UNVERIFIED·FAILED면 재시도를 소진한 것이다.
 * `reason_code`는 가드레일 ④와 같은 사유 코드 표(`PRECHECK_*`)다.
 */
export interface ExecutionVerificationHold {
  reason_code: string;
  /** 누적 조회 실패 횟수(첫 실패 포함). */
  attempts: number;
  first_failed_at: IsoDateTime;
  last_failed_at: IsoDateTime;
}

export interface ExecutionSummaryItem {
  execution_id: string;
  runbook_id: RunbookId;
  status: ExecutionStatus;
  /** 관제자 복구 조치 — 롤백 3종만 온다. */
  available_recovery_runbook_ids: RollbackRunbookId[];
  /** 보류가 없으면 null. UNVERIFIED면 반드시 있고 SUCCESS·ROLLBACK_INITIATED에는 오지 않는다(#249). */
  verification_hold?: ExecutionVerificationHold | null;
  updated_at: IsoDateTime;
}

/**
 * SSH 무차별 대입에서 **관측된** 공격자 IP. 대상은 인시던트의 `subject_arn`(그 EC2)이다.
 */
export interface SshBruteForceThreatContext {
  event_type: 'SSH_BRUTE_FORCE';
  source_ip: string;
}

/**
 * 보안 그룹 인그레스가 **허용한** 대역. 관측된 공격자 IP가 아니다 — `0.0.0.0/0`은 "전부 열려
 * 있다"는 뜻이지 누가 들어왔다는 뜻이 아니라, 화면은 두 값을 같은 말로 덮지 않는다.
 * 대상은 인시던트의 `subject_arn`(그 보안 그룹)이다.
 */
export interface OpenIpThreatContext {
  event_type: 'OPEN_IP';
  exposed_cidr: string;
}

/** `event_type`이 판별자다 — 위협 유형마다 싣는 값의 **의미**가 달라 한 필드로 합치지 않았다. */
export type ThreatContext = SshBruteForceThreatContext | OpenIpThreatContext;

/** 목록 항목 — 상세(IncidentResponse)의 부분집합. */
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
  /**
   * 저장된 위협 관측 1건에서 파생하는 문맥(#362 · PR #374). AI 분석 상태·근거 인용 여부와
   * 무관하게 실린다 — 분석 중이거나 실패해도 "어디서 들어왔나"는 남는다.
   *
   * **null은 문맥 부재이지 위협 없음 판정이 아니다.** FINOPS는 언제나 null이고, SECOPS도
   * 위협 연결 누락·대상 불일치·값 오류로 조회할 문맥이 없으면 null이다.
   */
  threat_context: ThreatContext | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

/**
 * 종료 처리 시 관제자가 남기는 판단 — **`JUSTIFIED` 1종이다.**
 * 모달의 다른 선택지 `과잉이었다`는 종료 값이 아니라 복구 실행으로 넘어가는 트리거라
 * 이 API에 도달하지 않는다(§4.6 · packages/schemas/api/incidents.py `ResolutionJudgement`).
 */
export const RESOLUTION_JUDGEMENTS = ['JUSTIFIED'] as const;
export type ResolutionJudgement = (typeof RESOLUTION_JUDGEMENTS)[number];

export interface IncidentResponse extends IncidentListItem {
  /** 분석 완료 시 정확히 3개, 분석 중·실패 시 빈 배열. */
  summary_lines: string[];
  evidence_ids: string[];
  recommendations: RecommendationItem[];
  executions: ExecutionSummaryItem[];
  /**
   * 종료 판단과 그 시각. **상세 전용**이라 목록 10필드에는 없다.
   * `status`가 `RESOLVED`인 것과 동시에 채워지고 그 전에는 둘 다 null이며,
   * 관제자 복구 접수로 재개되면 다시 null이 된다(ADR-0004).
   */
  resolution: ResolutionJudgement | null;
  resolved_at: IsoDateTime | null;
}

/** 목록 봉투 — 페이지네이션 필드는 Post-MVP. */
export interface IncidentsResponse {
  items: IncidentListItem[];
}

/* ─────────────────────────────── ws.py ─────────────────────────────── */
/* WS 이벤트 봉투. 계약 경로는 `/api/v1/ws`이고 REST와 같은 오리진에 붙는다(#168 · `routers/ws.py`). */

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

/* ───────────────────────────── metrics.py ───────────────────────────── */
/* GET /api/v1/metrics/timeseries — 대시보드 시계열 2축(DSH). 축마다 상태를 따로 받는다:
   CPU는 CloudWatch를 그 자리에서 부르고, SG 개방 건수는 적재된 판정 이력을 읽는다. */

/** 축 하나의 조회 결과. `UNAVAILABLE`이면 데이터는 비고 `reason_code`가 원인을 싣는다. */
export type AxisStatus = 'READY' | 'UNAVAILABLE';

export interface TimeseriesPoint {
  at: IsoDateTime;
  value: number;
}

/** EC2 한 대의 CPU 곡선. `name`은 Name 태그가 없으면 null — 화면은 `resource_id`로 대신한다. */
export interface CpuSeries {
  arn: string;
  resource_id: string;
  name: string | null;
  points: TimeseriesPoint[];
}

/** 축 1. 값은 CloudWatch 원계열이다 — 적재된 14일 롤링 평균(`metric_summaries`)이 아니다. */
export interface CpuAxis {
  status: AxisStatus;
  period_seconds: number | null;
  window_start: IsoDateTime | null;
  window_end: IsoDateTime | null;
  /** Rule Evaluator의 저활성 임계치. 임계선 값을 서버가 싣는다 — FE가 상수를 갖지 않는다. */
  idle_cpu_avg_threshold: number | null;
  series: CpuSeries[];
  reason_code: string | null;
}

/**
 * EC2 한 대의 네트워크 처리량 곡선. **In·Out이 한 자산 안에 짝으로 온다** — 화면이 두 방향을
 * 겹쳐 그려야 "받기만 하는가"가 읽히기 때문이다.
 *
 * 값의 단위는 **period당 바이트 총량**이다(CloudWatch `Sum`) — 표본값이 그 표본 구간에 오간
 * 바이트라, period 안에 표본이 여럿이면 합계만이 그 구간의 총량이 된다. 초당 처리량으로 읽으려면
 * 축의 `period_seconds`로 나눈다 — 나누는 쪽이 화면인 것은 서버가 관측값을 가공하지 않기 때문이다.
 */
export interface NetworkSeries {
  arn: string;
  resource_id: string;
  name: string | null;
  in_points: TimeseriesPoint[];
  out_points: TimeseriesPoint[];
}

/**
 * 축 3. CPU와 원천은 같은 CloudWatch지만 **호출이 갈려 있어 상태도 따로 온다** — 쿼리가
 * 인스턴스당 2개 더 붙는 쪽이라 이 축만 한도에 걸리는 경우가 있다.
 */
export interface NetworkAxis {
  status: AxisStatus;
  period_seconds: number | null;
  window_start: IsoDateTime | null;
  window_end: IsoDateTime | null;
  series: NetworkSeries[];
  reason_code: string | null;
}

/** 축 2. `value` = 그 회차에 `THREAT` 판정을 받은 SG 수(default SG는 화이트리스트로 빠진다). */
export interface SgExposureAxis {
  status: AxisStatus;
  points: TimeseriesPoint[];
  reason_code: string | null;
}

export interface MetricsTimeseriesResponse {
  generated_at: IsoDateTime;
  cpu: CpuAxis;
  network: NetworkAxis;
  sg_exposure: SgExposureAxis;
}
