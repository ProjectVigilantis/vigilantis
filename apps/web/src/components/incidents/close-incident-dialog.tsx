'use client';

import { useRef, useState } from 'react';
import { StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { ApiError, resolveIncident } from '@/lib/api/client';
import { arnShort, incidentTitle } from '@/lib/enum-labels';
import { closureAction, closureBlockedReason } from '@/lib/incident-closure';
import type { IncidentResponse } from '@/types/api';

export function CloseIncidentDialog({
  incident, locked = false, onClose, onResolved, onStale,
}: {
  incident: IncidentResponse;
  /** 실행 접수 직후 상세 재조회까지의 로컬 잠금. */
  locked?: boolean;
  onClose: () => void;
  onResolved: () => void;
  onStale: () => void;
}) {
  const [note, setNote] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const shownRisk = incident.reviewed_risk_level ?? incident.initial_risk_level;
  const action = closureAction(incident);
  const blockedReason = locked
    ? '진행 중인 실행이 끝난 뒤 종료할 수 있습니다.'
    : closureBlockedReason(incident);

  function close() {
    if (!inFlight.current) onClose();
  }

  async function submit() {
    if (inFlight.current || blockedReason) return;
    inFlight.current = true;
    setPending(true);
    setError('');
    try {
      await resolveIncident(incident.incident_id, 'NO_FURTHER_ACTION', note.trim() || null);
      onResolved();
    } catch (caught) {
      const apiError = caught instanceof ApiError
        ? caught : new ApiError(0, 'INTERNAL_ERROR', '요청을 보내지 못했습니다', '');
      // 서버 잠금 아래에서 승인·분석·종료 경합을 최종 판정한다. 낡은 화면은 다시 읽는다.
      if (apiError.code === 'INCIDENT_NOT_RESOLVABLE' || apiError.code === 'INCIDENT_NOT_FOUND') {
        onStale();
      } else {
        setError(apiError.message);
      }
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{action.label === '종료' ? '인시던트 종료' : action.label}</DialogTitle>
          <DialogDescription>{action.description}</DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">인시던트</dt>
          <dd className="font-medium">{incidentTitle(incident)}</dd>
          <dt className="text-muted-foreground">대상</dt>
          <dd className="truncate" title={incident.subject_arn}>{arnShort(incident.subject_arn)}</dd>
          {shownRisk !== null ? <>
            <dt className="text-muted-foreground">위험도</dt>
            <dd className="flex items-center gap-1">
              <StatusBadge field="risk_level" value={shownRisk} />
              {incident.reviewed_risk_level !== null && incident.reviewed_risk_level !== incident.initial_risk_level
                ? <span className="text-muted-foreground text-xs" title="AI 정밀 평가로 갱신된 값입니다">(정밀)</span>
                : null}
            </dd>
          </> : null}
        </dl>
        <p className="text-muted-foreground text-sm">현재 차단·설정은 변경하지 않습니다.</p>
        <details className="text-sm">
          <summary className="cursor-pointer">사유 남기기 (선택)</summary>
          <label className="mt-3 flex flex-col gap-2">
            종료 사유 (최대 1,000자)
            <textarea className="border-input bg-background min-h-20 rounded-md border p-2"
              value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000}
              disabled={pending} />
          </label>
        </details>
        {blockedReason ? <p role="status" className="text-muted-foreground text-sm">{blockedReason}</p> : null}
        {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={close} disabled={pending}>취소</Button>
          <Button type="button" onClick={submit} disabled={pending || !!blockedReason}>
            {pending ? '종료 처리 중…' : action.label}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
