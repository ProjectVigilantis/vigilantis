import type { ReactNode } from 'react';

import { formatKst } from '@/lib/utils';
import type {
  GuardDutyActivity,
  GuardDutyFact,
  IsolationRecommendationReason,
  RecommendationItem,
} from '@/types/api';

const REASONS: Record<IsolationRecommendationReason, string> = {
  GUARDDUTY_SSH_BRUTE_FORCE_ACTOR:
    '저장된 GuardDuty SSHBruteForce Instance 탐지에 ACTOR · NETWORK_CONNECTION · OUTBOUND와 유효한 원격 IP가 기록되어 있습니다.',
  TARGET_ADDRESS_NOT_PROVIDED: '저장된 탐지에 유효한 대상 주소가 제공되지 않았습니다.',
  RESOURCE_ROLE_TARGET: '저장된 탐지에서 이 자원은 TARGET으로 기록되어 있습니다. 이 사실만으로 격리 필요성을 판단할 수 없습니다.',
  RESOURCE_ROLE_UNKNOWN: '저장된 탐지의 자원 역할을 인식할 수 없습니다.',
  RESOURCE_ROLE_NOT_CAPTURED: '저장된 탐지에서 자원 역할을 확인할 수 없습니다.',
  FINDING_TYPE_NOT_COVERED: '이 탐지 유형은 조건부 격리 추천 규칙의 대상이 아닙니다.',
  DIRECTION_NOT_CONFIRMED: '저장된 탐지에서 네트워크 연결의 OUTBOUND 방향을 확인할 수 없습니다.',
  DIRECTION_CONFLICT: '저장된 자원 역할과 연결 방향이 조건부 격리 추천 조건에 맞지 않습니다.',
  EVIDENCE_TARGET_MISMATCH: '저장된 증거의 대상과 이 조치 후보의 대상이 일치하지 않습니다.',
};

function Fact({ fact }: { fact: GuardDutyFact<string | boolean> }) {
  switch (fact.status) {
    case 'RECORDED':
      return <>{String(fact.value)} · 기록됨</>;
    case 'ABSENT':
      return <>원천 누락</>;
    case 'INVALID':
      return <>유효하지 않은 원천 값</>;
    case 'UNRECOGNIZED':
      return <>인식 불가 · <code>{fact.source_value}</code></>;
  }
}

function SnapshotRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-x-3 gap-y-0.5 sm:grid-cols-[9rem_minmax(0,1fr)]">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 whitespace-pre-wrap break-all">{children}</dd>
    </div>
  );
}

function remoteAddressLabel(activity: GuardDutyActivity): string {
  const { resource_role: role, action_type: action, connection_direction: direction } = activity;
  if (role.status === 'RECORDED' && action.status === 'RECORDED'
    && action.value === 'NETWORK_CONNECTION' && direction.status === 'RECORDED') {
    if (role.value === 'ACTOR' && direction.value === 'OUTBOUND') return '기록된 대상 주소';
    if (role.value === 'TARGET' && direction.value === 'INBOUND') return '기록된 상대 주소';
  }
  return '원격 IP (원문 필드)';
}

/** 후보가 가진 저장분만 받는다. 인시던트 요약·최신 탐지로 후보 판단을 보충하지 않는다. */
export function CandidateRecommendation({ recommendation }: { recommendation: RecommendationItem }) {
  const basis = recommendation.recommendation_basis;
  const evidence = recommendation.recommendation_evidence;
  const evidenceIds = recommendation.evidence_ids ?? [];
  const conditional = basis?.classification === 'CONDITIONALLY_RECOMMENDED';
  const snapshotAnchor = evidence == null ? undefined
    : `candidate-evidence-${recommendation.runbook_id}-${encodeURIComponent(evidence.evidence_id)}`;

  return (
    <div className="flex min-w-0 flex-col gap-3 border-t pt-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-md border px-2 py-0.5 font-medium">
          {basis == null ? '저장된 후보 판단 없음'
            : conditional ? '조건부 추천' : '관제자 판단 필요'}
        </span>
        {basis != null ? <span className="text-muted-foreground text-xs">서버 규칙 · 저장된 판단</span> : null}
      </div>
      {basis == null ? (
        <p>이 후보에는 저장된 추천 판단이 없습니다. 탐지 사실이나 인시던트 요약으로 추천 여부를 추정하지 않습니다.</p>
      ) : (
        <>
          <p>{REASONS[basis.reason_code]}</p>
          {conditional ? (
            <div className="flex flex-col gap-1 rounded-md border p-3">
              <p className="font-medium">
                승인 전 확인: 이 탐지에 기록된 대상 주소로 접속할 정당한 업무 또는 승인된 테스트 사유가 없는지 관제자가 확인해야 합니다.
              </p>
              <p>그 확인을 전제로 한 조건부 격리 제안입니다. 침해 사실이나 즉시 격리 필요성을 증명하지 않습니다.</p>
            </div>
          ) : (
            <p>격리를 추천한다는 뜻이 아닙니다. 저장된 사실과 업무 맥락을 확인해 관제자가 조치 여부를 판단해야 합니다.</p>
          )}
          <dl className="flex min-w-0 flex-col gap-1 text-xs">
            <SnapshotRow label="판단 출처">{basis.origin}</SnapshotRow>
            <SnapshotRow label="저장된 규칙">{basis.rule_id} · v{basis.rule_version}</SnapshotRow>
            <SnapshotRow label="사유 코드">{basis.reason_code}</SnapshotRow>
            <SnapshotRow label="판단 시각">
              <time dateTime={basis.evaluated_at}>{formatKst(basis.evaluated_at)}</time>
            </SnapshotRow>
          </dl>
        </>
      )}
      <p className="text-muted-foreground text-xs">
        보호 태그·위험도·가드레일 통과는 제안을 허용하는 조건일 뿐, 격리 필요성의 근거가 아닙니다.
      </p>
      <div className="flex min-w-0 flex-col gap-1">
        <p className="text-muted-foreground text-xs">이 후보의 근거 ID</p>
        {evidenceIds.length === 0 ? <p>저장된 근거 ID 없음</p> : (
          <ul className="flex min-w-0 flex-col gap-1 text-xs">
            {evidenceIds.map((id) => (
              <li key={id} className="break-all">
                {evidence?.evidence_id === id ? (
                  <a href={`#${snapshotAnchor}`} className="underline underline-offset-4">
                    <code>{id}</code> · 저장된 탐지 보기
                  </a>
                ) : <><code>{id}</code> · 스냅샷 미제공</>}
              </li>
            ))}
          </ul>
        )}
      </div>
      {evidence == null ? <p className="text-muted-foreground">저장된 탐지 스냅샷 없음</p> : (
        <div id={snapshotAnchor} className="flex min-w-0 scroll-mt-4 flex-col gap-2 rounded-md border p-3">
          <p className="font-medium">이 후보의 저장된 탐지 스냅샷</p>
          <dl className="flex min-w-0 flex-col gap-1 text-xs">
            <SnapshotRow label="근거 ID">{evidence.evidence_id}</SnapshotRow>
            <SnapshotRow label="탐지 ID">{evidence.finding_id}</SnapshotRow>
            <SnapshotRow label="탐지 유형">{evidence.finding_type}</SnapshotRow>
            <SnapshotRow label="자원 유형">{evidence.resource_type}</SnapshotRow>
            <SnapshotRow label="증거 대상 ARN">{evidence.target_arn}</SnapshotRow>
            <SnapshotRow label="최초 관측">
              <time dateTime={evidence.first_seen_at}>{formatKst(evidence.first_seen_at)}</time>
            </SnapshotRow>
            <SnapshotRow label="최근 관측">
              <time dateTime={evidence.last_seen_at}>{formatKst(evidence.last_seen_at)}</time>
            </SnapshotRow>
            {evidence.activity == null ? <SnapshotRow label="활동 사실">미수집</SnapshotRow> : (
              <>
                <SnapshotRow label="자원 역할"><Fact fact={evidence.activity.resource_role} /></SnapshotRow>
                <SnapshotRow label="활동 유형"><Fact fact={evidence.activity.action_type} /></SnapshotRow>
                <SnapshotRow label="연결 방향"><Fact fact={evidence.activity.connection_direction} /></SnapshotRow>
                <SnapshotRow label={remoteAddressLabel(evidence.activity)}><Fact fact={evidence.activity.remote_ip} /></SnapshotRow>
                <SnapshotRow label="차단 기록 (Blocked)"><Fact fact={evidence.activity.blocked} /></SnapshotRow>
              </>
            )}
          </dl>
          <p className="text-muted-foreground text-xs">
            이 탐지 한 건의 기록이며 전체 공격자·대상 목록이나 현재 상태가 아닙니다. Blocked=false는 원문에 기록된 false이며 성공·안전을 뜻하지 않습니다.
          </p>
        </div>
      )}
    </div>
  );
}
