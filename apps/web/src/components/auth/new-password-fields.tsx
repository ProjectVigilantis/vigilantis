'use client';

// 새 비밀번호 + 확인 칸(#492 PR 3) — 최초 비밀번호 변경과 비밀번호 재설정 확인이 함께 쓴다.
//
// 비제어 입력이다. 값은 제출 순간 부모가 FormData(`new_password` · `confirm_password`)에서 읽고,
// 여기서는 입력할 때마다 조건 충족 여부(boolean)만 상태에 둔다 — 비밀번호 자체는 React 상태에 두지 않는다.
// 값을 다듬거나 막지 않는다(trim · 정규화 · 길이 자르기 없음, 붙여넣기 · 비밀번호 관리자 허용).
// 자동 입력이 input 이벤트를 내지 않아 표시가 늦어도 부모가 제출 때 같은 규칙(newPasswordError)으로 다시 판정한다.

import { AlertTriangle, Check, X } from 'lucide-react';
import { useRef, useState } from 'react';

import { INPUT_CLASS, LABEL_CLASS } from '@/components/form-classes';
import {
  PASSWORD_SYMBOLS,
  evaluatePassword,
  type PasswordChecks,
  type PasswordEvaluation,
} from '@/lib/password-policy';
import { cn } from '@/lib/utils';

const CHECKS: ReadonlyArray<readonly [keyof PasswordChecks, string]> = [
  ['length', '12자 이상 256자 이하'],
  ['upper', '영문 대문자(A–Z) 포함'],
  ['lower', '영문 소문자(a–z) 포함'],
  ['digit', '숫자(0–9) 포함'],
  ['symbol', '허용된 기호 포함'],
  ['allowedOnly', '영문 · 숫자 · 허용된 기호만 사용(공백 불가)'],
];

function Condition({ met, children }: { met: boolean; children: React.ReactNode }) {
  const Icon = met ? Check : X;
  return (
    <li className={cn('flex items-center gap-1.5', met ? 'text-foreground' : 'text-muted-foreground')}>
      <Icon aria-hidden className={cn('size-3.5 shrink-0', met ? 'text-emerald-600' : 'text-muted-foreground')} />
      <span>
        {children}
        <span className="sr-only">{met ? ' — 충족' : ' — 미충족'}</span>
      </span>
    </li>
  );
}

/** 허용되지 않은 문자 경고. 어떤 문자인지는 보여 주지 않는다 — 비밀번호 일부를 화면에 드러내지 않는다. */
function ForbiddenWarning({ id }: { id: string }) {
  return (
    <p id={id} role="status" className="flex items-center gap-1.5 text-xs text-destructive">
      <AlertTriangle aria-hidden className="size-3.5 shrink-0" />
      허용되지 않은 문자가 들어 있습니다. 공백 · 한글 · 전각 문자 등은 쓸 수 없습니다.
    </p>
  );
}

export function NewPasswordFields({
  idPrefix,
  disabled,
  autoFocus = false,
}: {
  idPrefix: string;
  disabled: boolean;
  autoFocus?: boolean;
}) {
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const [result, setResult] = useState<PasswordEvaluation>(() => evaluatePassword('', ''));
  const [confirmFilled, setConfirmFilled] = useState(false);

  function update() {
    const confirm = confirmRef.current?.value ?? '';
    setResult(evaluatePassword(passwordRef.current?.value ?? '', confirm));
    setConfirmFilled(confirm !== '');
  }

  const passwordId = `${idPrefix}-new-password`;
  const confirmId = `${idPrefix}-confirm-password`;
  const checksId = `${idPrefix}-password-checks`;
  const forbiddenId = `${idPrefix}-password-forbidden`;
  const confirmForbiddenId = `${idPrefix}-confirm-password-forbidden`;
  const matchId = `${idPrefix}-password-match`;

  return (
    <>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={passwordId} className={LABEL_CLASS}>
          새 비밀번호
        </label>
        <input
          ref={passwordRef}
          id={passwordId}
          name="new_password"
          type="password"
          autoComplete="new-password"
          required
          autoFocus={autoFocus}
          aria-describedby={result.hasForbidden ? `${checksId} ${forbiddenId}` : checksId}
          disabled={disabled}
          onInput={update}
          className={INPUT_CLASS}
        />
        {result.hasForbidden ? <ForbiddenWarning id={forbiddenId} /> : null}
        <ul id={checksId} className="flex flex-col gap-0.5 text-xs">
          {CHECKS.map(([key, label]) => (
            <Condition key={key} met={result.checks[key]}>
              {label}
            </Condition>
          ))}
        </ul>
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground select-none">허용된 기호 보기</summary>
          <code className="mt-1 block rounded-md bg-muted px-3 py-2 font-mono text-sm tracking-widest break-all">
            {PASSWORD_SYMBOLS}
          </code>
        </details>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={confirmId} className={LABEL_CLASS}>
          새 비밀번호 확인
        </label>
        <input
          ref={confirmRef}
          id={confirmId}
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          required
          aria-describedby={
            [result.confirmHasForbidden ? confirmForbiddenId : '', confirmFilled ? matchId : '']
              .filter(Boolean)
              .join(' ') || undefined
          }
          disabled={disabled}
          onInput={update}
          className={INPUT_CLASS}
        />
        {result.confirmHasForbidden ? <ForbiddenWarning id={confirmForbiddenId} /> : null}
        {confirmFilled ? (
          <ul id={matchId} className="text-xs">
            <Condition met={result.matches}>
              {result.matches ? '두 비밀번호가 같습니다' : '두 비밀번호가 다릅니다'}
            </Condition>
          </ul>
        ) : null}
      </div>
    </>
  );
}
