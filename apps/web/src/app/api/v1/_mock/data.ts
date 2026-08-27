// mock Route Handler가 공유하는 더미 데이터·in-memory 실행 저장소·오류 봉투 헬퍼입니다(SSOT 시연 시나리오 기준).

import {
  INCIDENT_STATUSES,
  RESPONSE_MODES,
  RISK_LEVELS,
  type AssetItem,
  type AssetsResponse,
  type ErrorCode,
  type ErrorResponse,
  type ExecutionStatus,
  type ExecutionSummaryItem,
  type IncidentResponse,
  type IncidentStatus,
  type IsoDateTime,
  type ResponseMode,
  type RollbackRunbookId,
  type RiskLevel,
  type RunbookId,
} from '@/types/api';

const ACCOUNT_ID = '123456789012';
const REGION = 'ap-northeast-2';

/**
 * 시드 시각은 모듈 로드 시점 기준 상대 시각이다(now-50분 ~ now-5분).
 * 절대 시각을 박으면 실행으로 갱신되는 updated_at이 시드보다 과거가 돼
 * "updated_at 최신 승" 병합 규칙을 mock으로 검증할 수 없다.
 */
const LOADED_AT = Date.now();

/** 계약 형식(초 단위 "Z")의 "n분 전". */
function minutesAgo(minutes: number): IsoDateTime {
  return new Date(LOADED_AT - minutes * 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

const COLLECTED_AT = minutesAgo(5);

const arn = {
  ec2Normal: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/i-0a1b2c3d4e5f60001`,
  ec2Idle: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/i-0a1b2c3d4e5f60002`,
  ec2Canary: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/i-0a1b2c3d4e5f60003`,
  sgOpenIp: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:security-group/sg-0a1b2c3d4e5f60001`,
  sgUnused: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:security-group/sg-0a1b2c3d4e5f60002`,
  sgQuarantine: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:security-group/sg-0a1b2c3d4e5f60003`,
  sgIsolation: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:security-group/sg-0a1b2c3d4e5f60004`,
  nacl: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:network-acl/acl-0a1b2c3d4e5f60001`,
  ebsAttached: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:volume/vol-0a1b2c3d4e5f60001`,
  ebsUnattached: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:volume/vol-0a1b2c3d4e5f60002`,
  asg: `arn:aws:autoscaling:${REGION}:${ACCOUNT_ID}:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/vigilantis-web-asg`,
  launchTemplate: `arn:aws:ec2:${REGION}:${ACCOUNT_ID}:launch-template/lt-0a1b2c3d4e5f60001`,
  targetGroup: `arn:aws:elasticloadbalancing:${REGION}:${ACCOUNT_ID}:targetgroup/vigilantis-web-tg/0a1b2c3d4e5f6000`,
} as const;

/* ────────────────────────────── GET /assets ────────────────────────────── */

const assetItems: AssetItem[] = [
  {
    // inc-20260814-0001의 RUNBOOK_EC2_ISOLATE가 SUCCESS로 끝나 격리 유지 중인 EC2.
    // 격리 = ALB 타겟 그룹 이탈 + 격리 SG 교체(PROJECT_STATUS.md 확정 결정 2026-08-25)이므로
    // REGISTERED_IN이 없고 SECURED_BY가 격리 SG를 가리킨다. 수집 시각(now-5분)이 격리(48분 전)보다
    // 뒤라 반영돼 있어야 한다. 웹 SG로 되돌리면 "격리했다"는 인시던트와 자산 화면이 어긋난다.
    // REGISTERED_IN 표본은 격리되지 않은 vigilantis-api-canary가 맡는다.
    arn: arn.ec2Normal,
    resource_id: 'i-0a1b2c3d4e5f60001',
    asset_type: 'EC2',
    resource_role: 'PRIMARY',
    name: 'vigilantis-web-01',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: 'running',
    spec: {
      instance_type: 't3.medium',
      availability_zone: `${REGION}a`,
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      subnet_id: 'subnet-0a1b2c3d4e5f60001',
      private_ip: '10.0.1.21',
    },
    relationships: [
      { relation_type: 'SECURED_BY', target_arn: arn.sgIsolation },
      { relation_type: 'ATTACHED_TO', target_arn: arn.ebsAttached },
      { relation_type: 'MEMBER_OF', target_arn: arn.asg },
      { relation_type: 'PROTECTED_BY', target_arn: arn.nacl },
    ],
    evaluation_status: 'COMPLETED',
    health_score: 78,
    verdict: 'SKIP',
    skip_reason_code: 'SKIP_ACTIVE',
    collected_at: COLLECTED_AT,
  },
  {
    // Idle EC2 — FinOps 최적화 후보. health_score는 서버에서 round(cpu_avg)라
    // COST_CANDIDATE(= cpu_avg < IDLE_CPU_AVG 5.0)면 5 미만이어야 한다.
    arn: arn.ec2Idle,
    resource_id: 'i-0a1b2c3d4e5f60002',
    asset_type: 'EC2',
    resource_role: 'PRIMARY',
    name: 'vigilantis-batch-01',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: 'running',
    spec: {
      instance_type: 't3.large',
      availability_zone: `${REGION}c`,
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      subnet_id: 'subnet-0a1b2c3d4e5f60002',
      private_ip: '10.0.2.34',
    },
    relationships: [
      { relation_type: 'SECURED_BY', target_arn: arn.sgOpenIp },
      { relation_type: 'PROTECTED_BY', target_arn: arn.nacl },
    ],
    evaluation_status: 'COMPLETED',
    health_score: 1,
    verdict: 'COST_CANDIDATE',
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    // 관측치 부족 + prod 태그 동시 — evaluate_ec2의 첫 분기가 이겨 SKIP_INSUFFICIENT_DATA다.
    // SKIP_PROD_PROTECTED로 보이면 서버 판정 순서가 뒤집힌 것.
    arn: arn.ec2Canary,
    resource_id: 'i-0a1b2c3d4e5f60003',
    asset_type: 'EC2',
    resource_role: 'PRIMARY',
    name: 'vigilantis-api-canary',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: 'running',
    spec: {
      instance_type: 't3.small',
      availability_zone: `${REGION}a`,
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      subnet_id: 'subnet-0a1b2c3d4e5f60001',
      private_ip: '10.0.1.55',
    },
    relationships: [
      { relation_type: 'SECURED_BY', target_arn: arn.sgOpenIp },
      { relation_type: 'REGISTERED_IN', target_arn: arn.targetGroup },
      { relation_type: 'PROTECTED_BY', target_arn: arn.nacl },
    ],
    evaluation_status: 'COMPLETED',
    health_score: 1,
    verdict: 'SKIP',
    skip_reason_code: 'SKIP_INSUFFICIENT_DATA',
    collected_at: COLLECTED_AT,
  },
  {
    // OpenIP SG — 0.0.0.0/0 SSH 인바운드 개방
    arn: arn.sgOpenIp,
    resource_id: 'sg-0a1b2c3d4e5f60001',
    asset_type: 'SG',
    resource_role: 'PRIMARY',
    name: 'vigilantis-web-sg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      description: 'web tier inbound',
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      attached: true,
      open_to_world: [{ protocol: 'tcp', from_port: 22, to_port: 22, ipv6: false }],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'THREAT',
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    // 미사용 SG — 어디에도 붙어 있지 않음
    arn: arn.sgUnused,
    resource_id: 'sg-0a1b2c3d4e5f60002',
    asset_type: 'SG',
    resource_role: 'PRIMARY',
    name: 'vigilantis-legacy-sg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      description: 'legacy batch tier',
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      attached: false,
      open_to_world: [],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'UNUSED',
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    // 미부착(UNUSED 조건) + 전체개방(THREAT 조건) 동시 — THREAT가 이긴다.
    // 이름이 default가 아니라 화이트리스트로도 빠지지 않는다. UNUSED로 보이면 판정 순서 회귀.
    arn: arn.sgQuarantine,
    resource_id: 'sg-0a1b2c3d4e5f60003',
    asset_type: 'SG',
    resource_role: 'PRIMARY',
    name: 'vigilantis-quarantine-sg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      description: 'detached quarantine tier',
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      attached: false,
      open_to_world: [
        { protocol: 'tcp', from_port: 3389, to_port: 3389, ipv6: false },
        { protocol: 'tcp', from_port: 22, to_port: 22, ipv6: true },
      ],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'THREAT',
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    // RUNBOOK_EC2_ISOLATE가 교체해 넣은 격리 SG. 인그레스 없음 + web-01에 부착 → SKIP_ACTIVE.
    // 주의: RUNBOOK_SG_DELETE_ISOLATED의 대상이 '격리용 SG'인지(enum-labels.ts 라벨 "격리 SG 삭제")
    // '고립(미부착) SG'인지(datasets/golden/README.md의 UNUSED 후보 서술) 문서로 확정된 바 없다.
    // 전자라면 inc-20260814-0001이 47분 전 지운 것이 이 SG라 목록에 남아 있으면 안 된다.
    arn: arn.sgIsolation,
    resource_id: 'sg-0a1b2c3d4e5f60004',
    asset_type: 'SG',
    resource_role: 'PRIMARY',
    name: 'vigilantis-isolation-sg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      description: 'quarantine SG applied by RUNBOOK_EC2_ISOLATE — no ingress',
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      attached: true,
      open_to_world: [],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'SKIP',
    skip_reason_code: 'SKIP_ACTIVE',
    collected_at: COLLECTED_AT,
  },
  {
    // 연결된 EBS
    arn: arn.ebsAttached,
    resource_id: 'vol-0a1b2c3d4e5f60001',
    asset_type: 'EBS',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-web-01-root',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: 'in-use',
    spec: {
      volume_type: 'gp3',
      size_gib: 30,
      availability_zone: `${REGION}a`,
      encrypted: true,
      attached_instance_ids: ['i-0a1b2c3d4e5f60001'],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'SKIP',
    skip_reason_code: 'SKIP_ACTIVE',
    collected_at: COLLECTED_AT,
  },
  {
    // 미연결 EBS — RUNBOOK_EBS_DELETE_UNATTACHED 대상
    arn: arn.ebsUnattached,
    resource_id: 'vol-0a1b2c3d4e5f60002',
    asset_type: 'EBS',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-orphan-vol',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: 'available',
    spec: {
      volume_type: 'gp2',
      size_gib: 100,
      availability_zone: `${REGION}c`,
      encrypted: false,
      attached_instance_ids: [],
    },
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: 'UNUSED',
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    arn: arn.nacl,
    resource_id: 'acl-0a1b2c3d4e5f60001',
    asset_type: 'NACL',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-public-nacl',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      vpc_id: 'vpc-0a1b2c3d4e5f60001',
      is_default: false,
      associated_subnet_ids: ['subnet-0a1b2c3d4e5f60001', 'subnet-0a1b2c3d4e5f60002'],
    },
    relationships: [],
    evaluation_status: 'NOT_APPLICABLE',
    health_score: null,
    verdict: null,
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    // ASG — 관계 6종 중 USES의 출발점
    arn: arn.asg,
    resource_id: 'vigilantis-web-asg',
    asset_type: 'AUTO_SCALING_GROUP',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-web-asg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      min_size: 1,
      max_size: 4,
      desired_capacity: 2,
      health_check_type: 'ELB',
    },
    relationships: [{ relation_type: 'USES', target_arn: arn.launchTemplate }],
    evaluation_status: 'NOT_APPLICABLE',
    health_score: null,
    verdict: null,
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    arn: arn.launchTemplate,
    resource_id: 'lt-0a1b2c3d4e5f60001',
    asset_type: 'LAUNCH_TEMPLATE',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-web-lt',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: { latest_version: 3, default_version: 3 },
    relationships: [],
    evaluation_status: 'NOT_APPLICABLE',
    health_score: null,
    verdict: null,
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
  {
    arn: arn.targetGroup,
    resource_id: 'vigilantis-web-tg',
    asset_type: 'ALB_TARGET_GROUP',
    resource_role: 'RUNBOOK_SUPPORT',
    name: 'vigilantis-web-tg',
    account_id: ACCOUNT_ID,
    region: REGION,
    state: null,
    spec: {
      protocol: 'HTTP',
      port: 80,
      target_type: 'instance',
      health_check_path: '/healthz',
    },
    relationships: [],
    evaluation_status: 'NOT_APPLICABLE',
    health_score: null,
    verdict: null,
    skip_reason_code: null,
    collected_at: COLLECTED_AT,
  },
];

export const assetsResponse: AssetsResponse = {
  collection_status: 'READY',
  last_collected_at: COLLECTED_AT,
  items: assetItems,
};

/* ───────────────────────────── GET /incidents ───────────────────────────── */

/** 시드 인시던트 5건. executions는 실행 mock이 직접 갱신하므로 mutable하다. */
export const incidents: IncidentResponse[] = [
  {
    // SECOPS High — SSH 브루트포스 선차단 격리 완료, 관제자 복구 가능
    incident_id: 'inc-20260814-0001',
    title: 'SSH 브루트포스 탐지 — vigilantis-web-01',
    subject_arn: arn.ec2Normal,
    category: 'SECOPS',
    status: 'RESOLVED',
    initial_risk_level: 'HIGH',
    reviewed_risk_level: 'HIGH',
    response_mode: 'PRE_MITIGATION_0_5S',
    summary_lines: [
      '외부 IP에서 vigilantis-web-01의 22번 포트로 5분간 반복 인증 실패가 관측됐습니다.',
      '위험도 High로 판정해 0.5초 선차단 경로로 인스턴스를 격리했습니다.',
      '격리 상태 유지 여부는 관제자 확인 후 원클릭 해제로 되돌릴 수 있습니다.',
    ],
    evidence_ids: ['ev-20260814-0001-01', 'ev-20260814-0001-02'],
    recommendations: [],
    executions: [
      {
        execution_id: 'exec-20260814-0001-01',
        runbook_id: 'RUNBOOK_EC2_ISOLATE',
        status: 'SUCCESS',
        available_recovery_runbook_ids: ['RUNBOOK_EC2_UNISOLATE'],
        updated_at: minutesAgo(48),
      },
      {
        execution_id: 'exec-20260814-0001-02',
        runbook_id: 'RUNBOOK_SG_DELETE_ISOLATED',
        status: 'SUCCESS',
        available_recovery_runbook_ids: ['RUNBOOK_SG_RECREATE'],
        updated_at: minutesAgo(47),
      },
    ],
    created_at: minutesAgo(50),
    updated_at: minutesAgo(47),
  },
  {
    // SECOPS Medium — AGENT_WAIT 승인 대기, 추천 2건
    incident_id: 'inc-20260814-0002',
    title: 'OpenIP(0.0.0.0/0) SSH 인바운드 노출 — vigilantis-web-sg',
    subject_arn: arn.sgOpenIp,
    category: 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: 'MEDIUM',
    reviewed_risk_level: 'MEDIUM',
    response_mode: 'AGENT_WAIT',
    summary_lines: [
      'vigilantis-web-sg가 0.0.0.0/0에 대해 22번 포트를 개방하고 있습니다.',
      '위험도 Medium으로 판정해 관제자 승인 대기 경로로 전환했습니다.',
      'NACL 차단 규칙 추가 또는 노출 보안 그룹 정리 중 하나를 승인해 주세요.',
    ],
    evidence_ids: ['ev-20260814-0002-01'],
    recommendations: [
      {
        runbook_id: 'RUNBOOK_NACL_ADD_DENY',
        target_arn: arn.nacl,
        // rule 100은 inc-20260814-0004가 같은 NACL에 이미 적용해 SUCCESS다. 번호가 겹치면
        // 이 추천을 승인하는 순간 기존 규칙과 충돌한다 — 다음 번호로 띄운다.
        display_parameters: { rule_number: '110', cidr_block: '0.0.0.0/0', protocol: 'tcp' },
      },
      {
        runbook_id: 'RUNBOOK_SG_DELETE_ISOLATED',
        target_arn: arn.sgOpenIp,
        // 삭제 대상 SG는 target_arn이 가리킨다 — 후보 파라미터가 비어 서버 파생본도 {}다.
        display_parameters: {},
      },
    ],
    executions: [],
    created_at: minutesAgo(40),
    updated_at: minutesAgo(39),
  },
  {
    // FINOPS — 위험도·response_mode 전부 null, title null(제목 없음 fallback 시연)
    incident_id: 'inc-20260814-0003',
    title: null,
    subject_arn: arn.ec2Idle,
    category: 'FINOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: null,
    reviewed_risk_level: null,
    response_mode: null,
    summary_lines: [
      'vigilantis-batch-01의 최근 14일 평균 CPU 사용률이 3% 미만입니다.',
      '현재 t3.large 스펙 대비 이용률이 낮아 최적화 후보로 판정했습니다.',
      't3.small 다운사이징 승인 시 스펙 JSON 백업 후 진행하며 Status Check 실패 시 자동 원복합니다.',
    ],
    evidence_ids: ['ev-20260814-0003-01', 'ev-20260814-0003-02'],
    recommendations: [
      {
        runbook_id: 'RUNBOOK_EC2_RIGHTSIZING',
        target_arn: arn.ec2Idle,
        // current_instance_type은 실행 파라미터에만 있고 후보에는 없다 — 서버가 내지 않는다(#183).
        display_parameters: { target_instance_type: 't3.small' },
      },
    ],
    executions: [],
    created_at: minutesAgo(30),
    updated_at: minutesAgo(29),
  },
  {
    // SECOPS — SSOT P0 시연 스토리 "NACL 차단 → 원클릭 해제".
    // 차단(NACL_ADD_DENY)은 SUCCESS로 끝났고, 해제(NACL_RESTORE)는 롤백 3종이 아니라
    // AI 추천 가능한 본편 조치라 available_recovery가 아닌 recommendations로 온다.
    incident_id: 'inc-20260814-0004',
    // v1.6 §4.4 규칙 2 — 제목은 **위협 이름**이다. 구 제목('NACL 인바운드 차단 적용 완료 —
    // 해제 대기')은 조치명이라 상태 배지가 이미 말하는 것을 한 번 더 말했다.
    // 위협 자체는 summary_lines[1]이 말하는 "vigilantis-web-01에 대한 비정상 접근 시도"다.
    title: '비정상 접근 시도 — vigilantis-web-01',
    subject_arn: arn.ec2Normal,
    category: 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: 'MEDIUM',
    // 차단이 적용돼 노출이 해소된 뒤의 AI 사후 재평가 결과
    reviewed_risk_level: 'LOW',
    response_mode: 'AGENT_WAIT',
    summary_lines: [
      'vigilantis-public-nacl에 0.0.0.0/0 인바운드 거부 규칙(rule 100)을 추가해 유입을 차단했습니다.',
      '차단 이후 vigilantis-web-01에 대한 비정상 접근 시도가 더 관측되지 않았습니다.',
      '정상 통신 영향이 없는지 확인한 뒤 원클릭 해제로 차단 규칙을 복원할 수 있습니다.',
    ],
    evidence_ids: ['ev-20260814-0004-01'],
    recommendations: [
      {
        runbook_id: 'RUNBOOK_NACL_RESTORE',
        target_arn: arn.nacl,
        // egress는 StrictBool이고 서버가 'true'/'false' 문자열로 내린다(_display_value).
        display_parameters: { rule_number: '100', egress: 'false' },
      },
    ],
    executions: [
      {
        execution_id: 'exec-20260814-0004-01',
        runbook_id: 'RUNBOOK_NACL_ADD_DENY',
        status: 'SUCCESS',
        available_recovery_runbook_ids: [],
        updated_at: minutesAgo(17),
      },
    ],
    created_at: minutesAgo(20),
    updated_at: minutesAgo(17),
  },
  {
    // FINOPS — 미연결 EBS 삭제 제안(성공 경로 확인용). 위험도 2필드·response_mode는 전부 null.
    incident_id: 'inc-20260814-0005',
    title: '미연결 EBS 볼륨 삭제 후보 — vigilantis-orphan-vol',
    subject_arn: arn.ebsUnattached,
    category: 'FINOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: null,
    reviewed_risk_level: null,
    response_mode: null,
    summary_lines: [
      'vigilantis-orphan-vol(100GiB gp2)이 어떤 인스턴스에도 연결돼 있지 않습니다.',
      '연결 이력이 없어 스토리지 비용만 발생하는 미사용 자산으로 판정했습니다.',
      '삭제 승인 시 스냅샷 백업 후 볼륨을 제거합니다.',
    ],
    evidence_ids: ['ev-20260814-0005-01'],
    recommendations: [
      {
        runbook_id: 'RUNBOOK_EBS_DELETE_UNATTACHED',
        target_arn: arn.ebsUnattached,
        // 삭제 대상 볼륨은 target_arn이 가리킨다 — 후보 파라미터가 비어 서버 파생본도 {}다.
        // 삭제 규모(size_gib)는 이 경로로 오지 않는다 — 승인 화면 복원은 #183 후속.
        display_parameters: {},
      },
    ],
    executions: [],
    created_at: minutesAgo(10),
    updated_at: minutesAgo(9),
  },
];

/* ───────────────────── 실행 mock 상태 (dev 서버 수명 기준) ───────────────────── */

export interface StoredExecution {
  incident_id: string;
  runbook_id: RunbookId;
  execution: ExecutionSummaryItem;
}

// ponytail: 프로세스 메모리 Map — dev 서버 재시작·핫리로드 시 리셋된다(mock 한정, 영속화 불필요).
export const executionsByIdempotencyKey = new Map<string, StoredExecution>();

/* ─────────────────── mock 전용 오버라이드 쿼리 (계약 밖) ─────────────────── */

/**
 * 시드만으로는 못 만드는 상태를 화면 개발용으로 강제하는 mock 전용 파라미터.
 * `?fail=internal`과 같은 계열이며 계약에 없는 이름이라 실 BE(FastAPI)는 무시한다.
 * 클라이언트(`src/lib/api/client.ts`)는 기본 경로에서 절대 붙이지 않는다.
 * 미등록 값은 조용히 무시된다(오류 응답은 계약 파라미터에만 준다).
 */
export interface MockIncidentOverrides {
  status?: IncidentStatus;
  risk?: RiskLevel;
  response_mode?: ResponseMode;
}

function pick<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return allowed.includes(value as T) ? (value as T) : undefined;
}

export function mockIncidentOverrides(params: URLSearchParams): MockIncidentOverrides {
  return {
    // 시드로 도달 불가한 2종만 — 나머지 3종은 강제 시 서버 불변식(제안 ≥1 등)을 깬다
    status: pick(
      params.get('mock_status'),
      INCIDENT_STATUSES.filter((s) => s === 'ANALYZING' || s === 'FAILED'),
    ),
    risk: pick(params.get('mock_risk'), RISK_LEVELS),
    response_mode: pick(params.get('mock_response_mode'), RESPONSE_MODES),
  };
}

/**
 * 시드 + 런타임 실행을 합친 인시던트 뷰.
 * 이미 실행된 제안은 recommendations에서 빠지고, status는 계약 불변식에 맞춰 재계산한다.
 */
/**
 * 원본 실행이 관제자에게 열어 주는 복구 조치(롤백 3종만).
 * 짝은 ADR-0004 표·SSOT §범위(P0 RIGHTSIZING+REVERT_SIZE, P1 SG_DELETE_ISOLATED+SG_RECREATE) 기준.
 * NACL_ADD_DENY의 해제(NACL_RESTORE)는 롤백이 아니라 본편 조치라 여기 오지 않는다(recommendations 경로).
 */
export const RECOVERY_BY_RUNBOOK: Partial<Record<RunbookId, RollbackRunbookId[]>> = {
  RUNBOOK_EC2_ISOLATE: ['RUNBOOK_EC2_UNISOLATE'],
  RUNBOOK_SG_DELETE_ISOLATED: ['RUNBOOK_SG_RECREATE'],
  RUNBOOK_EC2_RIGHTSIZING: ['RUNBOOK_EC2_REVERT_SIZE'],
};

/**
 * 복구를 열어 줄 수 있는 원본 상태 — AWS가 실제로 변경된 뒤여야 되돌릴 게 있다.
 * FAILED("AWS 변경 없이 실패")·IN_PROGRESS(아직 안 끝남)에는 복구 조치를 노출하지 않는다.
 * ROLLBACK_INITIATED는 자동 원복이 개시된 상태라 REVERT_SIZE 경로가 열려 있어야 한다.
 * 실 BE의 `EXECUTION_RECOVERABLE_STATUSES`와 같은 집합이다(PR #158).
 */
export const RECOVERABLE_ORIGIN_STATUSES = [
  'SUCCESS',
  'ROLLBACK_INITIATED',
] as const satisfies readonly ExecutionStatus[];

/**
 * 복구 목록은 **조회할 때마다 파생한다** — 저장해 두면 롤백이 끝난 원본에 값이 남아
 * "버튼은 보이는데 누르면 409"가 된다(PR #158 리뷰 포인트 4). 실 BE도 매 조회 재계산이다.
 * 조건 3개: 짝이 있고 · 원본이 복구 가능 상태이며 · 그 복구가 아직 접수되지 않았다.
 *
 * 세 번째 조건은 잉여가 아니다 — `SG_RECREATE` 접수 후 원본은 `ROLLBACK_INITIATED`가 되는데
 * 그 상태는 복구 가능 집합 **안**이라 상태 조건으로 막히지 않는다.
 *
 * 실 BE는 원본 실행 단위(`parent_execution_id`)로 판정하고 여기는 인시던트×runbook 단위로
 * 판정하는데, **같은 runbook의 재실행을 실행 라우터가 막고 있어서**(`execute/route.ts`가 이미
 * 실행된 runbook을 본편·복구 모두 409로 거절) 원본 결속 없이도 결과가 같다. mock에서 재실행을
 * 열면(격리 → 해제 → 다시 격리) 이 전제가 깨지므로 그때는 원본 결속을 도입해야 한다(PR #160 리뷰).
 */
function recoveryIds(
  execution: ExecutionSummaryItem,
  executed: Set<RunbookId>,
): RollbackRunbookId[] {
  if (!(RECOVERABLE_ORIGIN_STATUSES as readonly ExecutionStatus[]).includes(execution.status)) {
    return [];
  }
  return (RECOVERY_BY_RUNBOOK[execution.runbook_id] ?? []).filter((id) => !executed.has(id));
}

export function incidentView(
  incident: IncidentResponse,
  overrides: MockIncidentOverrides = {},
): IncidentResponse {
  const runtime = [...executionsByIdempotencyKey.values()]
    .filter((e) => e.incident_id === incident.incident_id)
    .map((e) => e.execution);
  const executions = [...incident.executions, ...runtime];
  const executed = new Set<RunbookId>(executions.map((e) => e.runbook_id));
  // 저장값이 아니라 지금 상태로 다시 계산한다. 항목을 복사하지 않고 제자리에서 갱신해야
  // 실행 라우터가 원본 상태를 갱신할 때 같은 객체를 잡는다.
  for (const execution of executions) {
    execution.available_recovery_runbook_ids = recoveryIds(execution, executed);
  }
  const recommendations = incident.recommendations.filter(
    (r) => !executed.has(r.runbook_id),
  );
  const inProgress = executions.some(
    (e) => e.status === 'IN_PROGRESS' || e.status === 'ROLLBACK_INITIATED',
  );
  // ROLLBACK_FAILED = AWS가 변경된 채 복구까지 실패(CRITICAL·수동 개입) — 흐름 진행 불가라
  // RESOLVED("더 진행할 제안·실행 없음")가 아니라 FAILED다. 남은 제안이 있으면 아직 진행 가능하므로 제외.
  const rollbackFailed = executions.some((e) => e.status === 'ROLLBACK_FAILED');
  const updatedAt = executions.reduce(
    (latest, e) => (e.updated_at > latest ? e.updated_at : latest),
    incident.updated_at,
  );
  const view: IncidentResponse = {
    ...incident,
    executions,
    recommendations,
    status: inProgress
      ? 'ACTION_IN_PROGRESS'
      : rollbackFailed && recommendations.length === 0
        ? 'FAILED'
        : incident.status === 'AWAITING_APPROVAL' && recommendations.length === 0
          ? 'RESOLVED'
          : incident.status,
    updated_at: updatedAt,
  };
  return applyOverrides(view, overrides);
}

/** 오버라이드 적용 — 강제한 상태에서도 서버 validator 불변식을 깨지 않는 범위로만 바꾼다. */
function applyOverrides(
  view: IncidentResponse,
  { status, risk, response_mode }: MockIncidentOverrides,
): IncidentResponse {
  // FINOPS는 위험도 2필드·response_mode가 전부 null이어야 한다(서버 불변식)
  if (view.category === 'SECOPS' && risk) {
    view = { ...view, initial_risk_level: risk, reviewed_risk_level: risk };
  }
  if (view.category === 'SECOPS' && response_mode) {
    view = { ...view, response_mode };
  }
  if (status === 'ANALYZING') {
    // 분석 미완 = 요약·제안 빈 배열, 실행 이력도 아직 없다
    return { ...view, status, summary_lines: [], recommendations: [], executions: [] };
  }
  if (status === 'FAILED') {
    // 흐름 진행 불가 = 남은 제안 없음. 수행된 조치 결과(executions)는 그대로 둔다
    return { ...view, status, recommendations: [] };
  }
  return view;
}

/* ───────────────────────────── 오류 봉투 헬퍼 ───────────────────────────── */

export function errorEnvelope(status: number, code: ErrorCode, message: string): Response {
  const body: ErrorResponse = {
    error: { code, message, request_id: `req-mock-${crypto.randomUUID()}` },
  };
  return Response.json(body, { status });
}
