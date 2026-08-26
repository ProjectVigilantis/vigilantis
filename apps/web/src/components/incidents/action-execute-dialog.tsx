'use client';

// ACT-001 원클릭 실행 확인 모달 — 화면설계서 v1.5 §4.6.
// 되돌리기 어려운 AWS 변경 직전에 대상과 결과를 확정하고, 중복 클릭을 여기서 차단합니다.

import { useState } from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ApiError, executeAction } from '@/lib/api/client';
import { DESTRUCTIVE_RUNBOOK_IDS, RUNBOOK_LABELS, isDestructiveRunbook } from '@/lib/enum-labels';
import type { ExecuteActionResponse, IncidentResponse, RunbookId } from '@/types/api';

/**
 * 파괴적 조치 경고는 **런북별로 다르다.** 2종에 한 문장을 공통으로 붙일 수 없다(PR #169 리뷰).
 *
 * - `RUNBOOK_EBS_DELETE_UNATTACHED` — 최종 스냅샷을 강제하지만 **등록된 롤백 런북이 없다**
 * - `RUNBOOK_SG_DELETE_ISOLATED` — 스냅샷이 아니라 **규칙 JSON 백업**(`SAVE_SG_FULL_RULES_JSON`)이고
 *   `RUNBOOK_SG_RECREATE`로 되돌릴 수 있다(`packages/schemas/runbooks.py` `ROLLBACK_RUNBOOK_BY_MAIN_ID`).
 *   대신 **신규 sg-id가 발급**돼 원본 sg-id를 참조하던 다른 규칙은 자동 복원되지 않는다.
 *
 * 이 모달은 관제자가 마지막으로 사실을 확인하는 자리다 — 없는 안전장치를 약속하지 않는다.
 */
const DESTRUCTIVE_WARNINGS: Record<(typeof DESTRUCTIVE_RUNBOOK_IDS)[number], string> = {
  RUNBOOK_EBS_DELETE_UNATTACHED:
    '볼륨을 삭제합니다. 삭제 직전 최종 스냅샷을 강제로 남기지만, 원클릭 롤백 런북은 없습니다.',
  RUNBOOK_SG_DELETE_ISOLATED:
    '보안 그룹을 삭제합니다. 삭제 직전 규칙 전체를 JSON으로 백업하고 「SG 재생성」으로 되돌릴 수 있습니다. 단 신규 sg-id가 발급되어, 원본 sg-id를 참조하던 다른 규칙은 자동 복원되지 않습니다.',
};

/** 실행 후보 1건. 주 조치는 `recommendations[]`의 값을 그대로 싣는다. */
export interface ActionCandidate {
  runbookId: RunbookId;
  /**
   * **실제로 바뀌는 자원**이다. `subject_arn`과 다를 수 있다 — 예를 들어 SG 인시던트의
   * `RUNBOOK_NACL_ADD_DENY`는 NACL을 고친다(PR #169 리뷰). 복구 런북은 계약에 이 값이 없어 null이다.
   */
  targetArn: string | null;
  /** 표시 전용. FE가 key 표시명을 지어내지 않고 원문을 쓴다. */
  displayParameters: Record<string, string> | null;
}

/**
 * 모달 한 인스턴스가 다루는 요청. **`idempotencyKey`는 모달을 열 때 만들어 여기 고정한다**(§4.6) —
 * 버튼 클릭 시점에 만들면 중복 클릭이 서로 다른 키가 되어 멱등성이 무력화된다.
 * 취소 후 재진입은 호출부가 새 객체를 만들므로 자연히 새 키가 된다.
 */
export interface ActionRequest {
  idempotencyKey: string;
  /** 실행 후보. 주 조치는 `recommendations`, 복구는 해제할 롤백 런북 1종. */
  candidates: ActionCandidate[];
  variant: 'ACTION' | 'RECOVERY';
  /** B 변형 표시용 — 어느 실행을 해제하는지. 전송하지 않는다(계약에서 폐기된 필드다). */
  originExecutionId?: string;
}

export interface ExecuteOutcome {
  execution: ExecuteActionResponse;
  /** 200(같은 키 재요청)으로 돌아온 경우. ACT-002가 `이미 접수된 요청입니다`를 함께 띄운다. */
  replayed: boolean;
}

/** §4.6 응답 처리 표의 오류 4종 — 모달을 유지할지, 닫고 상세를 재조회할지 가른다. */
function messageFor(error: ApiError): { text: string; keepOpen: boolean } {
  switch (error.code) {
    case 'IDEMPOTENCY_KEY_CONFLICT':
      return { text: '요청이 변경되었습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.', keepOpen: true };
    case 'PROPOSAL_NOT_EXECUTABLE':
      // 제안이 이미 실행됐거나 무효해진 경우 — 모달을 닫고 INC-002를 재조회한다(§4.6).
      return { text: '', keepOpen: false };
    case 'REQUEST_VALIDATION_FAILED':
      // 422는 계약 불일치다 — 재시도로 해결되지 않는다는 것을 문장으로 말한다(§4.6).
      return {
        text: `요청이 계약과 맞지 않습니다. 다시 시도해도 해결되지 않습니다 — ${error.message}`,
        keepOpen: true,
      };
    default:
      return { text: error.message, keepOpen: true };
  }
}

export function ActionExecuteDialog({
  incident,
  request,
  onClose,
  onExecuted,
  onProposalStale,
}: {
  incident: IncidentResponse;
  /** null이면 닫힌 상태다. 열 때마다 호출부가 새 객체(= 새 멱등 키)를 만든다. */
  request: ActionRequest | null;
  onClose: () => void;
  onExecuted: (outcome: ExecuteOutcome) => void;
  /** `409 PROPOSAL_NOT_EXECUTABLE` — 모달을 닫고 상세를 재조회한다. */
  onProposalStale: () => void;
}) {
  // ponytail: 후보가 여러 건이어도 **1건씩** 실행한다. 계약의 요청 본문은 `runbook_id` 단수이고
  // 멱등 키는 모달 인스턴스당 1개라, 한 번에 N건을 보내면 2번째부터 409 IDEMPOTENCY_KEY_CONFLICT가
  // 난다. §4.6 와이어프레임의 체크박스(패키지 실행)는 계약이 받쳐 주면 여기만 다중 선택으로 연다.
  const [selected, setSelected] = useState<RunbookId | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>('');

  const open = request !== null;
  const candidates = request?.candidates ?? [];
  const runbookId = selected ?? candidates[0]?.runbookId ?? null;
  const chosen = candidates.find((c) => c.runbookId === runbookId) ?? null;
  const isRecovery = request?.variant === 'RECOVERY';
  const destructive = runbookId !== null && isDestructiveRunbook(runbookId);

  function close() {
    setSelected(null);
    setPending(false);
    setError('');
    onClose();
  }

  async function submit() {
    if (request === null || runbookId === null) return;
    setPending(true);
    setError('');
    try {
      const outcome = await executeAction({
        // 전송은 3필드뿐이다 — 모달에 보이는 ARN·스펙·IP는 보내지 않는다(`extra=forbid` → 422).
        incident_id: incident.incident_id,
        runbook_id: runbookId,
        idempotency_key: request.idempotencyKey,
      });
      onExecuted(outcome);
      close();
    } catch (caught) {
      const apiError =
        caught instanceof ApiError
          ? caught
          : new ApiError(0, 'INTERNAL_ERROR', '요청을 보내지 못했습니다', '');
      const { text, keepOpen } = messageFor(apiError);
      if (!keepOpen) {
        close();
        onProposalStale();
        return;
      }
      setError(text);
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isRecovery ? '선제 차단을 해제합니다' : '선택한 조치를 실행합니다'}</DialogTitle>
          <DialogDescription>
            {isRecovery
              ? '해제하면 해당 대상의 접근이 다시 허용됩니다. 원본 차단 이력은 보존됩니다.'
              : 'Guardrail 4단계 통과 후 실행됩니다.'}
          </DialogDescription>
        </DialogHeader>

        <dl className="flex flex-col gap-2 text-sm">
          <div className="flex items-start justify-between gap-3">
            {/* 선택한 런북이 실제로 바꾸는 자원을 보여준다 — `subject_arn`과 다를 수 있다.
                복구 런북은 계약에 target이 없어 인시던트 대상으로 되돌린다. */}
            <dt className="text-muted-foreground">대상</dt>
            <dd className="text-right font-mono text-xs break-all">
              {chosen?.targetArn ?? incident.subject_arn}
            </dd>
          </div>
          {request?.originExecutionId ? (
            <div className="flex items-start justify-between gap-3">
              <dt className="text-muted-foreground">원본 실행</dt>
              <dd className="text-right font-mono text-xs">{request.originExecutionId}</dd>
            </div>
          ) : null}
        </dl>

        <fieldset className="flex flex-col gap-1.5">
          <legend className="text-muted-foreground mb-1.5 text-xs font-medium">
            {isRecovery ? '해제 대상' : `실행할 런북 (${candidates.length}건 중 선택)`}
          </legend>
          {candidates.map((candidate) => (
            <label
              key={candidate.runbookId}
              className="border-border flex items-start gap-2 rounded-md border p-2 text-sm"
            >
              <input
                type="radio"
                name="runbook"
                value={candidate.runbookId}
                checked={runbookId === candidate.runbookId}
                onChange={() => setSelected(candidate.runbookId)}
                disabled={pending}
                className="mt-1"
              />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium">{RUNBOOK_LABELS[candidate.runbookId]}</span>
                  {isDestructiveRunbook(candidate.runbookId) ? (
                    <span className="text-destructive text-xs font-medium">[파괴적]</span>
                  ) : null}
                </span>
                {/* display_parameters는 자유 형식 Record다 — key 표시명을 지어내지 않고 원문을 쓴다. */}
                {candidate.displayParameters
                  ? Object.entries(candidate.displayParameters).map(([key, value]) => (
                      <span key={key} className="text-muted-foreground flex gap-2 text-xs">
                        <span className="font-mono">{key}</span>
                        <span className="text-foreground">{value}</span>
                      </span>
                    ))
                  : null}
                {candidate.targetArn !== null ? (
                  <span className="text-muted-foreground font-mono text-xs break-all">
                    {candidate.targetArn}
                  </span>
                ) : null}
              </span>
            </label>
          ))}
        </fieldset>

        {/* 색만으로 표시하지 않는다 — 무엇이 삭제되고 되돌릴 수 없다는 것을 문장으로 말한다(§4.6). */}
        {destructive && runbookId !== null ? (
          <p className="border-destructive text-destructive rounded-md border p-3 text-sm">
            {/* 런북 표시명이 조사에 따라 갈리므로 "은/는"을 붙이지 않고 콜론으로 끊는다. */}
            ⚠ 파괴적 조치입니다 — <b>{RUNBOOK_LABELS[runbookId]}</b>:{' '}
            {DESTRUCTIVE_WARNINGS[runbookId as (typeof DESTRUCTIVE_RUNBOOK_IDS)[number]]}
          </p>
        ) : null}

        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        ) : null}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={close} disabled={pending}>
            취소
          </Button>
          {/* 첫 클릭 즉시 비활성 + 진행 표시로 전환한다(§4.6). */}
          <Button type="button" onClick={submit} disabled={pending || runbookId === null}>
            {pending ? '요청 중…' : isRecovery ? '해제 실행' : '실행'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
