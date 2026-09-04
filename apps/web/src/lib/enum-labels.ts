// enum 한글 표기 사전 — 화면설계서 v1.5 §3.2 표를 계약 타입(@/types/api)에서 파생해 한 곳에 모읍니다.

import type {
  AssetItem,
  AssetType,
  CollectionStatus,
  EvaluationStatus,
  ExecutionStatus,
  IncidentCategory,
  IncidentListItem,
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
  SKIP_UNSUPPORTED_STATE: { label: '판정 보류 상태', tone: 'yellow' }, // EBS 전이·비정상·미상(available/in-use 외) — 삭제 후보 아님
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
  // 조치는 끝났고 관제자 종료 판단만 남은 자리 — 실패가 아니므로 '진행 불가'(빨강)와
  // 색이 갈려야 한다. v1.6 ④ [종료 판단] 모달이 열리는 상태다 (#240).
  AWAITING_CLOSURE: { label: '종료 판단 대기', tone: 'green' },
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

/** 3.2.1 Runbook 사전의 "표시" 열. 파라미터·실행 상세는 서버 계약(schemas) 소관이라 옮기지 않는다. */
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
 * 자원을 삭제해 되돌릴 수 없는 런북 2종 — ACT-001 경고 블록의 **유일한 판별 근거**다(§4.6).
 *
 * 런북별 `destructive` 플래그는 만들지 않기로 확정됐고(BE 2026-08-20), Whitelist가 10종 고정이라
 * 화면이 ID로 판별해도 위험하지 않다. `available_recovery_runbook_ids`는 "백업 기반 롤백이 붙어
 * 있느냐"만 나타내고(`RUNBOOK_NACL_ADD_DENY`는 목록이 비어도 복원 가능), 계약의 `risk_level`은
 * **위협 위험도이지 조치 위험도가 아니다** — 둘 다 판별에 쓸 수 없다.
 */
export const DESTRUCTIVE_RUNBOOK_IDS = [
  'RUNBOOK_EBS_DELETE_UNATTACHED',
  'RUNBOOK_SG_DELETE_ISOLATED',
] as const satisfies readonly RunbookId[];

export function isDestructiveRunbook(id: RunbookId): boolean {
  return (DESTRUCTIVE_RUNBOOK_IDS as readonly string[]).includes(id);
}

/**
 * `state`는 AWS 원문 문자열(nullable)이라 Record로 못 만든다 — 3.2 표의 규칙만 옮긴다.
 * `running`만 번역하고 나머지는 원문 그대로 노출한다(임의 번역 금지).
 */
export function assetStateLabel(state: string | null): EnumLabel | null {
  if (state === null) return null;
  if (state === 'running') return { label: '실행 중', tone: 'green' };
  return { label: state, tone: 'gray' };
}

/**
 * 상태 표시 규칙 — 미연결 EBS는 비용이 계속 청구되므로 `available`을 원문대로 두지 않는다(§4.2).
 * 원문만 보면 "available = 정상"으로 읽혀 낭비 자산이 정상으로 보인다.
 * 목록 카드와 상세 패널이 같은 문구를 써야 해서 사전에 둔다.
 */
export function assetStateEntry(asset: AssetItem): EnumLabel | null {
  if (asset.asset_type === 'EBS' && asset.spec.attached_instance_ids.length === 0) {
    return { label: '미연결 (비용 발생)', tone: 'orange' };
  }
  return assetStateLabel(asset.state);
}

/**
 * 4.3 `spec` key-value 표기명. 자산 유형별 spec 모델(계약)의 필드 합집합이며,
 * `vpc_id`·`availability_zone`처럼 여러 유형이 공유하는 key는 한 항목으로 둔다.
 * 사전에 없는 key는 계약이 늘었다는 뜻이라 화면이 key 원문을 그대로 출력한다(누락 은폐 금지).
 */
export const SPEC_KEY_LABELS: Record<string, string> = {
  instance_type: '인스턴스 유형',
  availability_zone: '가용 영역',
  vpc_id: 'VPC',
  subnet_id: '서브넷',
  private_ip: '사설 IP',
  description: '설명',
  attached: 'SG 연결 여부',
  open_to_world: '전체 개방 포트',
  is_default: '기본 NACL',
  associated_subnet_ids: '연결된 서브넷',
  volume_type: '볼륨 유형',
  size_gib: '크기 (GiB)',
  encrypted: '암호화',
  attached_instance_ids: '연결된 인스턴스',
  min_size: '최소 용량',
  max_size: '최대 용량',
  desired_capacity: '희망 용량',
  health_check_type: '헬스 체크 유형',
  latest_version: '최신 버전',
  default_version: '기본 버전',
  protocol: '프로토콜',
  port: '포트',
  target_type: '대상 유형',
  health_check_path: '헬스 체크 경로',
};

/**
 * ARN의 마지막 세그먼트. 카드의 `대상` 줄과 `incidentTitle` fallback이 같은 축약을 쓴다 —
 * 정의가 두 곳에 있으면 두 화면이 같은 자산을 다르게 부르게 된다(PR #171 리뷰).
 */
export function arnShort(arn: string): string {
  // `.pop()`은 구분자로 끝나는 ARN에서 빈 문자열을 낼 수 있어 `||`로 원문을 남긴다.
  return arn.split(/[:/]/).pop() || arn;
}

/**
 * 4.4·4.5 확정 — `title`은 nullable이고 빈 문자열은 오지 않는다.
 * null이면 유형 표시명 + `subject_arn` 축약으로 대체한다. 화면마다 다른 문구를 쓰지 않도록 여기 둔다.
 */
export function incidentTitle(incident: IncidentListItem): string {
  if (incident.title !== null) return incident.title;
  const label = CATEGORY_LABELS[incident.category]?.label ?? incident.category;
  return `${label} · ${arnShort(incident.subject_arn)}`;
}
