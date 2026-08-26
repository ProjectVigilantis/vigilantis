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
import { RUNBOOK_LABELS, isDestructiveRunbook } from '@/lib/enum-labels';
import type { ExecuteActionResponse, IncidentResponse, RunbookId } from '@/types/api';

/**
 * 모달 한 인스턴스가 다루는 요청. **`idempotencyKey`는 모달을 열 때 만들어 여기 고정한다**(§4.6) —
 * 버튼 클릭 시점에 만들면 중복 클릭이 서로 다른 키가 되어 멱등성이 무력화된다.
 * 취소 후 재진입은 호출부가 새 객체를 만들므로 자연히 새 키가 된다.
 */
export interface ActionRequest {
  idempotencyKey: string;
  /** 실행 후보. 주 조치는 `recommendations`의 런북, 복구는 해제할 롤백 런북 1종. */
  candidates: RunbookId[];
  variant: 'ACTION' | 'RECOVERY';
  /** B 변형 표시용 — 어느 실행을 해제하는지. 전송하지 않는다(계약에서 폐기된 필드다). */
  originExecutionId?: string;
}

export interface ExecuteOutcome {
  execution: ExecuteActionResponse;
  /** 200(같은 키 재요청)으로 돌아온 경우. ACT-002가 `이미 접수된 요청입니다`를 함께 띄운다. */
  replayed: boolean;
}

/** §4.7 최종 상태 4종. 여기서는 `PROPOSAL_NOT_EXECUTABLE` 후 재조회 판단에만 쓴다. */
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
  const runbookId = selected ?? candidates[0] ?? null;
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
            <dt className="text-muted-foreground">대상</dt>
            <dd className="text-right font-mono text-xs break-all">{incident.subject_arn}</dd>
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
          {candidates.map((id) => (
            <label key={id} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="runbook"
                value={id}
                checked={runbookId === id}
                onChange={() => setSelected(id)}
                disabled={pending}
              />
              <span>{RUNBOOK_LABELS[id]}</span>
              {isDestructiveRunbook(id) ? (
                <span className="text-destructive text-xs font-medium">[파괴적]</span>
              ) : null}
            </label>
          ))}
        </fieldset>

        {/* 색만으로 표시하지 않는다 — 무엇이 삭제되고 되돌릴 수 없다는 것을 문장으로 말한다(§4.6). */}
        {destructive && runbookId !== null ? (
          <p className="border-destructive text-destructive rounded-md border p-3 text-sm">
            {/* 런북 표시명이 조사에 따라 갈리므로 "은/는"을 붙이지 않고 콜론으로 끊는다. */}
            ⚠ 파괴적 조치입니다 — <b>{RUNBOOK_LABELS[runbookId]}</b>: 자원을 삭제하며{' '}
            <b>되돌릴 수 없습니다.</b> 삭제 직전 최종 스냅샷을 강제 생성합니다.
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
