// enum 한글 표기 사전 — 화면설계서 v1.5 §3.2 표를 계약 타입(@/types/api)에서 파생해 한 곳에 모읍니다.

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
  RunbookId,
  SkipReasonCode,
  Verdict,
} from '@/types/api';

/**
 * 화면설계서 3.2 표의 "색" 열. 실제 CSS 클래스 매핑은 StatusBadge가 소유한다
 * (사전은 의미만, 스타일은 컴포넌트 — 색 정의가 두 곳에 흩어지지 않게).
 */
export type LabelTone =
  | 'neutral'
  | 'gray'
  | 'yellow'
  | 'orange'
  | 'red'
  | 'blue'
  | 'green'
  | 'purple';

export interface EnumLabel {
  /** 화면에 그대로 출력하는 표시명. */
  label: string;
  tone: LabelTone;
  /** 3.2의 "회색 + 스피너" — 진행 중 상태. */
  spinner?: boolean;
  /** 5장 표의 상태 기호. 실행 status 전용(Incident status와 시각적으로 구분). */
  glyph?: string;
}

/**
 * `null` = 3.2 표의 "—"(배지 미표시). 화면은 이 경우 아무것도 그리지 않는다.
 * Record라서 계약 enum에 값이 추가되면 이 파일에서 컴파일 에러가 난다.
 */
type LabelMap<K extends string> = Record<K, EnumLabel | null>;

/** 3.3 값 없음 처리 — 게이지·수치를 0으로 그리지 않고 이 문자를 쓴다. */
export const NO_VALUE = '—';

export const ASSET_TYPE_LABELS: LabelMap<AssetType> = {
  EC2: { label: 'EC2', tone: 'neutral' },
  SG: { label: '보안 그룹', tone: 'neutral' },
  EBS: { label: 'EBS 볼륨', tone: 'neutral' },
  NACL: { label: 'NACL', tone: 'neutral' },
  AUTO_SCALING_GROUP: { label: 'Auto Scaling 그룹', tone: 'neutral' },
  LAUNCH_TEMPLATE: { label: '시작 템플릿', tone: 'neutral' },
  ALB_TARGET_GROUP: { label: '대상 그룹', tone: 'neutral' },
};

export const RESOURCE_ROLE_LABELS: LabelMap<ResourceRole> = {
  PRIMARY: null, // 주요 관제 자산 — 배지 미표시
  RUNBOOK_SUPPORT: { label: '지원 자산', tone: 'gray' },
};

export const COLLECTION_STATUS_LABELS: LabelMap<CollectionStatus> = {
  NOT_COLLECTED: { label: '수집 전', tone: 'gray' },
  COLLECTING: { label: '수집 중', tone: 'gray', spinner: true },
  READY: null,
  PARTIAL: { label: '일부만 수집됨', tone: 'yellow' },
  FAILED: { label: '수집 실패', tone: 'red' },
};

export const EVALUATION_STATUS_LABELS: LabelMap<EvaluationStatus> = {
  NOT_APPLICABLE: null, // 판정 영역 자체를 렌더하지 않는다
  PENDING: { label: '판정 대기', tone: 'gray' },
  COMPLETED: null, // verdict로 표시
  FAILED: { label: '판정 실패', tone: 'red' },
};

export const VERDICT_LABELS: LabelMap<Verdict> = {
  COST_CANDIDATE: { label: '최적화 후보', tone: 'orange' },
  THREAT: { label: '위협', tone: 'red' },
  UNUSED: { label: '미사용', tone: 'orange' },
  SKIP: { label: '제외', tone: 'gray' }, // 사유는 skip_reason_code 배지를 함께 표시
};

/**
 * v1.5에서 5종을 색으로 분리했다(§3.2). 전부 회색이면 "왜 제외됐는지"를 배지 텍스트로만
 * 읽어야 하는데, 제외 사유는 후속 조치가 서로 다르다 — 데이터 부족은 재수집으로 풀리고
 * 운영 보호는 정책이라 안 풀린다. 색이 그 차이를 먼저 알려준다.
 */
export const SKIP_REASON_LABELS: LabelMap<SkipReasonCode> = {
  SKIP_INSUFFICIENT_DATA: { label: '데이터 부족', tone: 'yellow' }, // 데이터 문제 — 재수집으로 풀림
  SKIP_PROD_PROTECTED: { label: '운영 보호 대상', tone: 'blue' }, // 정책상 보호
  SKIP_LOW_UTIL: { label: '저사용 임계 미달', tone: 'orange' }, // 기준만 못 넘음 — 재검토 여지
  SKIP_WHITELISTED: { label: '예외 등록됨', tone: 'purple' }, // 사람이 등록한 예외
  SKIP_ACTIVE: { label: '활성 자산', tone: 'green' }, // 정상 사용 중
};

export const CATEGORY_LABELS: LabelMap<IncidentCategory> = {
  FINOPS: { label: '최적화', tone: 'blue' },
  SECOPS: { label: '보안', tone: 'red' },
};

/** Incident status — 실행 status와 의미가 다르므로 사전도 배지도 분리한다(3.2). */
export const INCIDENT_STATUS_LABELS: LabelMap<IncidentStatus> = {
  ANALYZING: { label: '분석 중', tone: 'gray', spinner: true },
  AWAITING_APPROVAL: { label: '승인 대기', tone: 'yellow' },
  ACTION_IN_PROGRESS: { label: '조치 진행 중', tone: 'blue' },
  RESOLVED: { label: '종료', tone: 'gray' },
  FAILED: { label: '진행 불가', tone: 'red' },
};

export const RISK_LEVEL_LABELS: LabelMap<RiskLevel> = {
  HIGH: { label: '높음', tone: 'red' },
  MEDIUM: { label: '중간', tone: 'orange' },
  LOW: { label: '낮음', tone: 'yellow' },
};

export const RESPONSE_MODE_LABELS: LabelMap<ResponseMode> = {
  PRE_MITIGATION_0_5S: { label: '선제 차단됨', tone: 'red' },
  AGENT_WAIT: { label: '승인 대기', tone: 'yellow' },
  TIMEOUT_ISOLATION_1M: { label: '시간 초과 자동 격리', tone: 'red' },
};

/**
 * 실행 status 6종 — 라벨은 4.7 "진행 표시기 매핑", 기호·색은 5장과 4.7 "최종 상태 표시".
 * FAILED(AWS 변경 없음)와 ROLLBACK_FAILED(변경된 채 복구 실패·CRITICAL)를 합치지 않는다.
 */
export const EXECUTION_STATUS_LABELS: LabelMap<ExecutionStatus> = {
  // IN_PROGRESS 색은 문서에 없다 — 3.2의 진행 중 표기(회색 + 스피너) 관례를 따른다
  IN_PROGRESS: { label: '실행 중', tone: 'gray', spinner: true },
  SUCCESS: { label: '완료', tone: 'green', glyph: '✓' },
  FAILED: { label: '실패', tone: 'red', glyph: '✕' },
  ROLLBACK_INITIATED: { label: '복구 중', tone: 'orange', glyph: '⟲' },
  ROLLED_BACK: { label: '복구 완료', tone: 'blue', glyph: '⟲' },
  ROLLBACK_FAILED: { label: '복구 실패', tone: 'red', glyph: '⚠' },
};

/** 3.2.1 Runbook 사전의 "표시" 열. 파라미터·실행 상세는 런북 명세서 소관이라 옮기지 않는다. */
export const RUNBOOK_LABELS: Record<RunbookId, string> = {
  RUNBOOK_EC2_ISOLATE: 'EC2 격리',
  RUNBOOK_NACL_ADD_DENY: 'NACL DENY 추가',
  RUNBOOK_NACL_RESTORE: 'NACL 복원',
  RUNBOOK_SG_DELETE_ISOLATED: '격리 SG 삭제',
  RUNBOOK_EC2_RIGHTSIZING: 'EC2 스펙 조정',
  RUNBOOK_EC2_ENABLE_AUTOSCALING: 'Auto Scaling 전환',
  RUNBOOK_EBS_DELETE_UNATTACHED: '미연결 EBS 삭제',
  RUNBOOK_EC2_UNISOLATE: 'EC2 격리 해제',
  RUNBOOK_SG_RECREATE: 'SG 재생성',
  RUNBOOK_EC2_REVERT_SIZE: '이전 스펙 복원',
};

/**
 * `state`는 AWS 원문 문자열(nullable)이라 Record로 못 만든다 — 3.2 표의 규칙만 옮긴다.
 * `running`만 번역하고 나머지는 원문 그대로 노출한다(임의 번역 금지).
 */
export function assetStateLabel(state: string | null): EnumLabel | null {
  if (state === null) return null;
  if (state === 'running') return { label: '실행 중', tone: 'green' };
  return { label: state, tone: 'gray' };
}
