// INC-001 명함 카드 — 인시던트 1건. 화면설계서 v1.5 §4.4 데이터 바인딩 표를 따릅니다.

import Link from 'next/link';

import { RISK_BAND_CLASS, RISK_BAND_EMPTY, StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { incidentTitle } from '@/lib/enum-labels';
import { cn, formatKst } from '@/lib/utils';
import type { IncidentListItem } from '@/types/api';

/**
 * 전이 배지 2개 — `[초기] → [정밀]`(§7.3 축약). 두 위험도를 합치지 않으므로(§7.2)
 * 색 하나로 표현할 수 없어 좌측 띠(훑어보기)와 이 배지(정확히 읽기)로 나눠 담는다.
 *
 * `reviewed_risk_level = null`은 **색 없는 테두리**로 그린다 — 색이 없다는 것 자체가
 * "아직 판정 안 됨"이다(§4.4). 문구는 status로 가른다: 분석 중이면 `평가 중`, 분석이
 * 깨졌으면 `평가 실패`, 그 외는 아직 안 나온 것이므로 `평가 없음`이다.
 * (§4.4는 앞의 둘만 적었는데, 승인 대기 중 reviewed가 비어 있는 건은 실패가 아니다.)
 */
function RiskTransition({ incident }: { incident: IncidentListItem }) {
  if (incident.initial_risk_level === null) return null;

  const pendingLabel =
    incident.status === 'ANALYZING'
      ? '평가 중'
      : incident.status === 'FAILED'
        ? '평가 실패'
        : '평가 없음';

  return (
    <span className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">위험도</span>
      <StatusBadge field="risk_level" value={incident.initial_risk_level} />
      <span className="text-muted-foreground" aria-hidden>
        →
      </span>
      {incident.reviewed_risk_level !== null ? (
        <StatusBadge field="risk_level" value={incident.reviewed_risk_level} />
      ) : (
        <span className="border-border text-muted-foreground rounded-md border px-1.5 py-0.5">
          {pendingLabel}
        </span>
      )}
    </span>
  );
}

export function IncidentCard({
  incident,
  /** `승인 대기` 프리셋에서만 붙는다 — 전체 목록에서는 직접 실행하지 않는다(§4.4 액션). */
  showExecute = false,
}: {
  incident: IncidentListItem;
  showExecute?: boolean;
}) {
  const band =
    incident.initial_risk_level === null
      ? RISK_BAND_EMPTY
      : RISK_BAND_CLASS[incident.initial_risk_level];

  return (
    <Card className="relative gap-3 overflow-hidden p-4 pl-5 transition-colors hover:border-ring">
      {/* 좌측 세로 띠 — 정렬 축과 같은 불변 키라 그리드가 위에서부터 색 순서로 흐른다(§4.4). */}
      <span aria-hidden className={cn('absolute inset-y-0 left-0 w-1.5', band)} />

      <div className="flex flex-wrap items-center gap-1">
        <StatusBadge field="category" value={incident.category} />
        <StatusBadge field="incident_status" value={incident.status} />
        {/* B-Medium 승인 대기 행 식별 — 계약 필드 그대로다(§4.4 바인딩).
            `AGENT_WAIT`의 표시명이 `AWAITING_APPROVAL`과 똑같은 `승인 대기`라(3.2 사전)
            배지 두 개가 나란히 붙으면 중복으로 읽힌다. INC-002는 `대응` 행 안에 있어 문제가
            없으므로, 사전을 고치지 않고 카드에서만 `대응` 접두를 붙여 갈라 읽게 한다. */}
        {incident.response_mode !== null ? (
          <span className="flex items-center gap-1">
            <span className="text-muted-foreground text-xs">대응</span>
            <StatusBadge field="response_mode" value={incident.response_mode} />
          </span>
        ) : null}
      </div>

      {/* 카드 전체가 INC-002 진입점이다(§4.4 액션). 링크를 `::after`로 카드 전면에 펼쳐
          어디를 눌러도 상세로 가되, 접근성 트리에는 **링크 하나**만 남긴다 —
          카드를 role="button"으로 감싸면 안에 든 [조치 실행]이 중첩 대화형 요소가 된다.
          버튼은 아래에서 `z-10`으로 이 오버레이 위에 올린다(PR #171 리뷰). */}
      <Link
        href={`/incidents/${encodeURIComponent(incident.incident_id)}`}
        className="hover:text-primary min-w-0 font-medium underline-offset-4 after:absolute after:inset-0 hover:underline"
      >
        <span className="line-clamp-2">{incidentTitle(incident)}</span>
      </Link>

      <RiskTransition incident={incident} />

      {/* 카드는 ARN을 마지막 세그먼트로 자르므로 전체 ARN을 볼 방법이 이 `title` 하나다.
          링크 오버레이(`::after`)가 위치 지정 자손이라 이 문단을 통째로 덮어 툴팁이 뜨지 않았다
          (PR #171 리뷰). `z-10`으로 올리되 `w-fit`으로 텍스트 폭만 덮어, 남는 여백에서는
          카드 클릭이 그대로 살아 있게 한다. */}
      <p
        className="text-muted-foreground relative z-10 w-fit max-w-full truncate text-xs"
        title={incident.subject_arn}
      >
        대상 {incident.subject_arn.split(/[:/]/).pop() ?? incident.subject_arn}
      </p>

      {/* `mt-auto` — FINOPS는 위험도 행이 없어 내용이 한 줄 짧다. 그리드가 카드 높이만 맞추고
          푸터가 따라오지 않으면 같은 행의 [조치 실행] 버튼 줄이 들쭉날쭉해진다(PR #171 리뷰). */}
      <div className="border-border/60 mt-auto flex items-center justify-between gap-2 border-t pt-3">
        <span className="text-muted-foreground text-xs">{formatKst(incident.updated_at)}</span>
        {showExecute ? (
          // 실행은 ACT-001 모달(#166)이 열어야 한다 — 요청 3필드·멱등 키·파괴적 조치 경고가
          // 그 카드 소관이고, 그것 없이 실행을 열면 계약을 어긴 요청이 나간다.
          // `z-10`으로 카드 전면 링크 오버레이 위에 둔다.
          <Button type="button" size="sm" disabled className="relative z-10">
            조치 실행
          </Button>
        ) : null}
      </div>
    </Card>
  );
}
