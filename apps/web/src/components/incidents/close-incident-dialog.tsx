'use client';

// ACT-001 C 종료 확인 모달 — 화면설계서 v1.6 §4.6 (v1.6 팀 회의 결정).
// A·B와 달리 AWS를 바꾸지 않는다 — 인시던트 상태를 옮기는 자리라 부를 API가 다르고, 아직 없다.
//
// **종료는 판단이 끝난 뒤에 온다.** 회의 결정의 순서는
//   선제차단 → 관제자가 유지/해제 판단 → 종료 → 히스토리
// 이므로 남은 제안이 있으면 아직 판단이 끝나지 않은 것이라 종료를 막는다.
// 계약의 `RESOLVED`(= "더 진행할 제안·실행 없음")와 같은 뜻이다.

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
import { arnShort, incidentTitle } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { IncidentResponse } from '@/types/api';

/**
 * 선제 차단의 정당성 판단 2택. 기본값은 **`정당했다`** — 선제 차단은 정책이 시킨 일이고(§7.1)
 * 뒤집는 쪽이 예외다. `과잉이었다`는 종료하지 않고 **해제 흐름으로 넘긴다**: 해제는 실행이라
 * 결과를 보고 나서 종료를 판단해야 한다(§4.6).
 */
type Verdict = 'JUSTIFIED' | 'EXCESSIVE';

const CHOICES: { value: Verdict; label: string; hint: string }[] = [
  { value: 'JUSTIFIED', label: '정당했다', hint: '격리를 유지한 채 종료합니다.' },
  { value: 'EXCESSIVE', label: '과잉이었다', hint: '종료하지 않고 격리 해제로 넘어갑니다.' },
];

export function CloseIncidentDialog({
  incident,
  onClose,
  onChooseRecovery,
}: {
  incident: IncidentResponse;
  onClose: () => void;
  /** `과잉이었다`를 고르면 해제 흐름으로 넘긴다 — 이 모달은 아무것도 실행하지 않는다. */
  onChooseRecovery: () => void;
}) {
  const [verdict, setVerdict] = useState<Verdict>('JUSTIFIED');
  const remaining = incident.recommendations.length;

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>이 인시던트를 종료합니다</DialogTitle>
          <DialogDescription>
            선제 차단이 정당했는지 판단합니다. 이 모달은 AWS를 바꾸지 않습니다.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">위협</dt>
          <dd className="font-medium">{incidentTitle(incident)}</dd>
          <dt className="text-muted-foreground">대상</dt>
          <dd className="truncate" title={incident.subject_arn}>
            {arnShort(incident.subject_arn)}
          </dd>
        </dl>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-sm font-medium">선제 차단이 정당했는지 고릅니다</legend>
          {CHOICES.map((choice) => (
            <label
              key={choice.value}
              className={cn(
                'flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors',
                verdict === choice.value ? 'border-ring bg-muted/50' : 'hover:bg-muted/30',
              )}
            >
              <input
                type="radio"
                name="close-verdict"
                className="mt-1"
                value={choice.value}
                checked={verdict === choice.value}
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
            그대로 덮인다 — 원래 피드백("제안이 남아있으면 종료로 폐기되지 않도록")이 막으려던 것이다. */}
        {remaining > 0 ? (
          <p className="border-border bg-muted/40 rounded-md border p-3 text-xs">
            ⚠ 남은 제안 {remaining}건 — 아직 종료할 수 없습니다.
            <span className="text-muted-foreground block">
              먼저 실행하거나, 실행하지 않기로 판단해 제안을 정리한 뒤 종료합니다.
            </span>
          </p>
        ) : null}

        <p className="text-muted-foreground text-xs">
          종료해도 카드 이름은 바뀌지 않습니다 — 상태 배지만 「종료」가 됩니다.
        </p>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            취소
          </Button>
          {verdict === 'EXCESSIVE' ? (
            <Button type="button" onClick={onChooseRecovery}>
              해제로 넘어가기
            </Button>
          ) : (
            // 막히는 이유가 둘이라 문구를 가른다 — 하나는 **지금 관제자가 풀 수 있는 것**(남은
            // 제안 정리)이고 하나는 **BE 대기**(9장 #33)다. 합치면 관제자가 자기가 할 수 있는
            // 일을 백엔드 탓으로 읽는다.
            <Button
              type="button"
              disabled
              title={
                remaining > 0
                  ? '남은 제안을 먼저 정리해야 종료할 수 있습니다'
                  : '종료 처리 API 대기 중 (9장 #33)'
              }
            >
              종료 처리
            </Button>
          )}
        </DialogFooter>
        {verdict === 'JUSTIFIED' && remaining === 0 ? (
          <p className="text-muted-foreground text-right text-xs">
            ⏳ 종료 처리 API 대기 — 상태를 `RESOLVED`로 바꾸는 엔드포인트가 아직 없습니다 (9장 #33).
          </p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
