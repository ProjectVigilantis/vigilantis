'use client';

// ACT-001 C 종료 확인 모달 — 화면설계서 v1.6 §4.6 (v1.6 팀 회의 결정).
// A·B와 달리 AWS를 바꾸지 않는다 — 인시던트 상태를 옮기는 자리라 부르는 API가 다르다:
// `POST /api/v1/incidents/{id}/resolve` (#199, 멱등 키 없음 — 조건부 갱신 자체가 멱등이다).
//
// **종료는 판단이 끝난 뒤에 온다.** 회의 결정의 순서는
//   선제차단 → 관제자가 유지/해제 판단 → 종료 → 히스토리
// 이므로 남은 제안이 있으면 아직 판단이 끝나지 않은 것이라 종료를 막는다.
// 계약의 `RESOLVED`(= "더 진행할 제안·실행 없음")와 같은 뜻이다.
//
// **두 카테고리가 같은 모달을 쓴다.** FINOPS도 RIGHTSIZING 종료 판정으로 `AWAITING_CLOSURE`에
// 들어오므로(workflows.py `judge_rightsizing_boot`) 여기서 열리지 않으면 그 상태가 막다른 길이
// 된다. 구조는 같고 **문구만** 갈린다 — 판단 대상이 `선제 차단`이냐 `수행된 조치`냐의 차이다.

import { useRef, useState } from 'react';

import { StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ApiError, resolveIncident } from '@/lib/api/client';
import { arnShort, incidentTitle } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { IncidentCategory, IncidentResponse } from '@/types/api';

/**
 * 정당성 판단 2택. 기본값은 **`정당했다`** — 수행된 대응은 정책이 시킨 일이고(§7.1)
 * 뒤집는 쪽이 예외다. `과잉이었다`는 종료하지 않고 **복구 흐름으로 넘긴다**: 복구는 실행이라
 * 결과를 보고 나서 종료를 판단해야 한다(§4.6). 그래서 계약의 `ResolutionJudgement`는
 * `JUSTIFIED` 1종이며 `EXCESSIVE`는 API에 도달하지 않는다.
 */
type Verdict = 'JUSTIFIED' | 'EXCESSIVE';

/** 카테고리별로 갈리는 것은 문구뿐이다 — 선택지·흐름·계약은 같다. */
const COPY: Record<IncidentCategory, {
  /** "…가 정당했는지 판단합니다"의 주어. 조사까지 포함한다. */
  subject: string;
  /** 첫 줄 라벨 — FINOPS 건을 `위협`이라 부르면 틀린 말이 된다. */
  titleLabel: string;
  justifiedHint: string;
  excessiveHint: string;
  recoveryCta: string;
}> = {
  SECOPS: {
    subject: '선제 차단이',
    titleLabel: '위협',
    justifiedHint: '격리를 유지한 채 종료합니다.',
    excessiveHint: '종료하지 않고 격리 해제로 넘어갑니다.',
    recoveryCta: '해제로 넘어가기',
  },
  FINOPS: {
    subject: '수행된 조치가',
    titleLabel: '인시던트',
    justifiedHint: '변경을 유지한 채 종료합니다.',
    excessiveHint: '종료하지 않고 원상 복구로 넘어갑니다.',
    recoveryCta: '복구로 넘어가기',
  },
};

/** 409·404는 화면이 이미 낡았다는 뜻이라 모달을 닫고 상세를 재조회한다(§4.6 응답 처리). */
function messageFor(error: ApiError): { text: string; keepOpen: boolean } {
  switch (error.code) {
    case 'INCIDENT_NOT_RESOLVABLE':
    case 'INCIDENT_NOT_FOUND':
      return { text: '', keepOpen: false };
    case 'REQUEST_VALIDATION_FAILED':
      return {
        text: `요청이 계약과 맞지 않습니다. 다시 시도해도 해결되지 않습니다 — ${error.message}`,
        keepOpen: true,
      };
    default:
      return { text: error.message, keepOpen: true };
  }
}

export function CloseIncidentDialog({
  incident,
  onClose,
  onResolved,
  onStale,
  onChooseRecovery,
}: {
  incident: IncidentResponse;
  onClose: () => void;
  /** 종료 처리 성공 — 호출부가 모달을 닫고 상세를 재조회한다. */
  onResolved: () => void;
  /** 409·404 — 화면이 낡았다. 모달을 닫고 상세를 재조회한다. */
  onStale: () => void;
  /** `과잉이었다`를 고르면 복구 흐름으로 넘긴다 — 이 모달은 AWS를 바꾸지 않는다. */
  onChooseRecovery: () => void;
}) {
  const [verdict, setVerdict] = useState<Verdict>('JUSTIFIED');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  /** 중복 클릭의 **동기 잠금** — `pending`은 리렌더 전까지 버튼을 못 닫는다(#217). */
  const inFlight = useRef(false);

  const copy = COPY[incident.category];
  const shownRisk = incident.reviewed_risk_level ?? incident.initial_risk_level;
  const remaining = incident.recommendations.length;
  /**
   * 되돌릴 수 있는 실행이 하나도 없으면 `과잉이었다`는 갈 곳이 없다 — 계약이 복구를
   * **실행 항목별**로 매달기 때문이다(§4.5). 막지 않으면 버튼이 아무 반응 없이 닫힌다.
   * 롤백 런북이 없는 `RUNBOOK_EBS_DELETE_UNATTACHED`와 실행 전 `FAILED`가 이 경우다.
   */
  const recoverable = incident.executions.some(
    (e) => e.available_recovery_runbook_ids.length > 0,
  );
  const blocked = remaining > 0;

  const choices: { value: Verdict; label: string; hint: string; disabled: boolean }[] = [
    { value: 'JUSTIFIED', label: '정당했다', hint: copy.justifiedHint, disabled: false },
    {
      value: 'EXCESSIVE',
      label: '과잉이었다',
      hint: recoverable ? copy.excessiveHint : '되돌릴 수 있는 실행이 없어 고를 수 없습니다.',
      disabled: !recoverable,
    },
  ];

  function close() {
    inFlight.current = false;
    setPending(false);
    setError('');
    onClose();
  }

  async function submit() {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError('');
    try {
      // 보내는 값은 `JUSTIFIED` 하나다 — `EXCESSIVE`는 이 버튼에 도달하지 않는다.
      await resolveIncident(incident.incident_id, 'JUSTIFIED');
      close();
      onResolved();
    } catch (caught) {
      const apiError =
        caught instanceof ApiError
          ? caught
          : new ApiError(0, 'INTERNAL_ERROR', '요청을 보내지 못했습니다', '');
      const { text, keepOpen } = messageFor(apiError);
      if (!keepOpen) {
        close();
        onStale();
        return;
      }
      setError(text);
      setPending(false);
      inFlight.current = false;
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>이 인시던트를 종료합니다</DialogTitle>
          <DialogDescription>
            {copy.subject} 정당했는지 판단합니다. 이 모달은 AWS를 바꾸지 않습니다.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">{copy.titleLabel}</dt>
          <dd className="font-medium">{incidentTitle(incident)}</dd>
          <dt className="text-muted-foreground">대상</dt>
          <dd className="truncate" title={incident.subject_arn}>
            {arnShort(incident.subject_arn)}
          </dd>
          {/* 위험도는 "그 차단이 정당했나"를 가르는 값이라 판단 화면에 반드시 있어야 한다(§4.6).
              표기는 목록 카드와 같은 단일 값이다(§7.3) — 두 판정을 대조하는 자리는 INC-002 상세다.
              FINOPS는 위험도 2필드가 계약상 null이라 이 줄이 통째로 빠진다. */}
          {shownRisk !== null ? (
            <>
              <dt className="text-muted-foreground">위험도</dt>
              <dd className="flex flex-wrap items-center gap-1">
                <StatusBadge field="risk_level" value={shownRisk} />
                {/* 표식 규칙은 목록 카드의 RiskLevelLine과 같다 — 초기 판정 그대로면 없다. */}
                {incident.reviewed_risk_level !== null &&
                incident.reviewed_risk_level !== incident.initial_risk_level ? (
                  <span className="text-muted-foreground text-xs" title="AI 정밀 평가로 갱신된 값입니다">
                    (정밀)
                  </span>
                ) : null}
              </dd>
            </>
          ) : null}
        </dl>

        <fieldset className="flex flex-col gap-2" disabled={pending}>
          <legend className="mb-2 text-sm font-medium">{copy.subject} 정당했는지 고릅니다</legend>
          {choices.map((choice) => (
            <label
              key={choice.value}
              className={cn(
                'flex items-start gap-3 rounded-md border p-3 text-sm transition-colors',
                choice.disabled
                  ? 'text-muted-foreground cursor-not-allowed opacity-60'
                  : 'cursor-pointer',
                verdict === choice.value ? 'border-ring bg-muted/50' : 'hover:bg-muted/30',
              )}
            >
              <input
                type="radio"
                name="close-verdict"
                className="mt-1"
                value={choice.value}
                checked={verdict === choice.value}
                disabled={choice.disabled}
                onChange={() => setVerdict(choice.value)}
              />
              <span>
                <span className="font-medium">{choice.label}</span>
                <span className="text-muted-foreground block text-xs">{choice.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>

        {/* 남은 제안이 있으면 **종료를 막는다**(§4.6 종료 조건).
            제안이 남았다는 것은 아직 판단이 끝나지 않았다는 뜻이고, 여기서 종료하면 그 제안이
            그대로 덮인다 — 서버도 종료 시 남은 제안을 INVALIDATED로 정리한다(workflows.py).
            화면이 서버보다 엄격한 자리다: 계약은 AWAITING_APPROVAL에서의 종료를 허용한다. */}
        {blocked ? (
          <p className="border-border bg-muted/40 rounded-md border p-3 text-xs">
            ⚠ 남은 제안 {remaining}건 — 아직 종료할 수 없습니다.
            <span className="text-muted-foreground block">
              먼저 실행하거나, 실행하지 않기로 판단해 제안을 정리한 뒤 종료합니다.
            </span>
          </p>
        ) : null}

        {error ? (
          <p role="alert" className="text-destructive text-xs">
            {error}
          </p>
        ) : null}

        <p className="text-muted-foreground text-xs">
          종료해도 카드 이름은 바뀌지 않습니다 — 상태 배지만 「종료」가 됩니다.
        </p>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={close} disabled={pending}>
            취소
          </Button>
          {verdict === 'EXCESSIVE' ? (
            <Button type="button" onClick={onChooseRecovery}>
              {copy.recoveryCta}
            </Button>
          ) : (
            <Button
              type="button"
              onClick={submit}
              disabled={blocked || pending}
              title={blocked ? '남은 제안을 먼저 정리해야 종료할 수 있습니다' : undefined}
            >
              {pending ? '종료 처리 중…' : '종료 처리'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
