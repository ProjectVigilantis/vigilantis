// 인시던트 명함 카드 1건 — 화면설계서 v1.6 §4.4 데이터 바인딩 표를 따릅니다.
// INC-001 보안과 INC-004 자산이 같은 카드를 쓴다 — 값이 없는 자리(위험도·선제차단)만 비운다.

import Link from 'next/link';

import { RISK_BAND_CLASS, RISK_BAND_EMPTY, StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { arnShort, incidentTitle } from '@/lib/enum-labels';
import { isPreemptive } from '@/lib/incident-filter';
import { cn, formatKst } from '@/lib/utils';
import type { IncidentListItem } from '@/types/api';

/**
 * 목록 카드의 위험도 — **값 하나**만 그린다 (v1.6, 2026-08-27 PM 결정).
 *
 * 초기 판정으로 시작해 **AI 정밀 평가가 나오면 그 값으로 바뀐다**(`reviewed ?? initial`).
 * v1.5까지는 `[초기] → [정밀]` 전이 배지 2개였는데(구 §7.3), 두 값이 같을 때
 * `중간 → 중간`처럼 같은 말이 두 번 나왔다.
 *
 * **정보가 사라지지는 않는다** — 두 판정을 나란히 대조하는 자리는 INC-002 상세의
 * `위험도 판정` 2칸이며 거기는 그대로다(§4.5). 목록은 훑어보는 자리고 상세는 정확히 읽는
 * 자리라는 §4.4의 분업을 따른다.
 *
 * 정밀 평가가 아직 없으면 초기 판정을 그대로 쓴다 — 구 버전의 `평가 중`·`평가 실패` 문구는
 * 값이 하나가 되면서 자리를 잃었고, 그 정보는 **상태 배지**가 이미 말한다(`분석 중`·`진행 불가`).
 */
function RiskLevelLine({ incident }: { incident: IncidentListItem }) {
  const shown = incident.reviewed_risk_level ?? incident.initial_risk_level;
  if (shown === null) return null; // FINOPS — 계약이 두 위험도를 null로 강제한다

  return (
    <span className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">위험도</span>
      <StatusBadge field="risk_level" value={shown} />
      {/* 정밀 평가로 바뀐 건임을 한 글자로만 밝힌다 — 어느 판정을 보고 있는지 모르면
          상세의 2칸과 대조할 때 혼란이 된다. 초기 판정 그대로면 표식이 없다. */}
      {incident.reviewed_risk_level !== null &&
      incident.reviewed_risk_level !== incident.initial_risk_level ? (
        <span className="text-muted-foreground" title="AI 정밀 평가로 갱신된 값입니다">
          (정밀)
        </span>
      ) : null}
    </span>
  );
}

export function IncidentCard({
  incident,
  /** `승인 대기` 프리셋에서만 붙는다 — 전체 목록에서는 직접 실행하지 않는다(§4.4 액션). */
  showExecute = false,
  /** 이 카드의 상세를 조회하는 중. **누른 카드만** 잠근다(다른 카드는 계속 눌린다). */
  executePending = false,
  onExecute,
}: {
  incident: IncidentListItem;
  showExecute?: boolean;
  executePending?: boolean;
  onExecute?: (incidentId: string) => void;
}) {
  // 띠도 표시값을 따른다 — 배지와 띠가 서로 다른 판정을 가리키면 카드가 자기모순이 된다.
  //
  // ⚠️ 정렬 축은 `initial_risk_level`(불변 키) 그대로다(§4.4). 정밀 평가로 값이 바뀐 카드는
  // 색과 자리가 어긋날 수 있다. 정렬을 reviewed로 옮기면 AI 평가가 갱신될 때마다 목록이
  // 눈앞에서 재배치돼 누르려던 항목이 움직인다 — 설계서가 명시적으로 피한 쪽이다.
  const bandLevel = incident.reviewed_risk_level ?? incident.initial_risk_level;
  const band = bandLevel === null ? RISK_BAND_EMPTY : RISK_BAND_CLASS[bandLevel];

  return (
    <Card className="relative gap-3 overflow-hidden p-4 pl-5 transition-colors hover:border-ring">
      {/* 좌측 세로 띠 — 정렬 축과 같은 불변 키라 그리드가 위에서부터 색 순서로 흐른다(§4.4). */}
      <span aria-hidden className={cn('absolute inset-y-0 left-0 w-1.5', band)} />

      <div className="flex flex-wrap items-center gap-1">
        <StatusBadge field="category" value={incident.category} />
        <StatusBadge field="incident_status" value={incident.status} />
        {/* v1.6 §4.4 — `response_mode` 배지는 **선제차단 계열에만** 붙인다.
            `AGENT_WAIT`의 표시명이 `AWAITING_APPROVAL`과 똑같은 `승인 대기`라(3.2 사전)
            같은 카드에 "승인 대기"가 두 번 뜬다. v1.5는 `대응` 접두를 붙여 갈랐지만, 실제 화면에서
            `보안 · 승인 대기 · 대응 승인 대기`로 읽혀 접두가 중복을 없애지 못했다.
            그 정보는 상태 배지가 이미 말한다.

            선제차단 계열은 반대다 — 상태 배지만으로는 "승인 없이 이미 격리됐다"를 알 수 없고,
            같은 이름의 프리셋이 있어 어느 카드가 거기 담기는지 카드에 보여야 한다. */}
        {incident.response_mode !== null && isPreemptive(incident) ? (
          <StatusBadge field="response_mode" value={incident.response_mode} />
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

      <RiskLevelLine incident={incident} />

      {/* 카드는 ARN을 마지막 세그먼트로 자르므로 전체 ARN을 볼 방법이 이 `title` 하나다.
          링크 오버레이(`::after`)가 위치 지정 자손이라 이 문단을 통째로 덮어 툴팁이 뜨지 않았다
          (PR #171 리뷰). `z-10`으로 올리되 `w-fit`으로 텍스트 폭만 덮어, 남는 여백에서는
          카드 클릭이 그대로 살아 있게 한다. */}
      <p
        className="text-muted-foreground relative z-10 w-fit max-w-full truncate text-xs"
        title={incident.subject_arn}
      >
        대상 {arnShort(incident.subject_arn)}
      </p>

      {/* `mt-auto` — FINOPS는 위험도 행이 없어 내용이 한 줄 짧다. 그리드가 카드 높이만 맞추고
          푸터가 따라오지 않으면 같은 행의 [조치 실행] 버튼 줄이 들쭉날쭉해진다(PR #171 리뷰). */}
      <div className="border-border/60 mt-auto flex items-center justify-between gap-2 border-t pt-3">
        {/* v1.6 §4.4 — 카드 날짜는 **`created_at`**(이슈가 올라온 시각)이다. `updated_at`을 쓰면
            상태가 바뀔 때마다 날짜가 움직여 "언제 들어온 건인가"를 못 읽는다. 정렬 동점 기준도
            `created_at`이라 화면과 순서가 같은 값을 가리킨다.

            **상대 표기("오늘이면 hh:mm")를 쓰지 않는다.** "지금"이 필요해 SSR과 하이드레이션이
            다른 문자열을 그린다(설계서 v1.6 §4.4 — 이 이유로 규칙을 절대 표기로 바꿨다).
            `formatKst`는 KST 고정이라 그 함정이 없다. */}
        <span className="text-muted-foreground text-xs">{formatKst(incident.created_at)}</span>
        {showExecute ? (
          // 누르면 ACT-001 모달을 연다(§4.6). 목록 계약에 `recommendations`가 없어
          // 호출부가 `GET /incidents/{id}`로 후보를 채운 뒤 연다.
          // `z-10`으로 카드 전면 링크 오버레이 위에 둔다.
          <Button
            type="button"
            size="sm"
            className="relative z-10"
            disabled={executePending || onExecute === undefined}
            onClick={() => onExecute?.(incident.incident_id)}
          >
            {executePending ? '여는 중…' : '조치 실행'}
          </Button>
        ) : null}
      </div>
    </Card>
  );
}
