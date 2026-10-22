'use client';

// 한글이 들어가면 한/영 전환을 확인하라고 알려 주는 입력(#492 PR 3). 로그인 비밀번호와 이메일 칸(로그인 · 재설정 · 초대)에만 쓴다.
// 비제어 입력이다 — 값을 바꾸거나 막지 않고, 상태에는 "한글이 있는가" boolean만 둔다. 제출도 막지 않는다
// (이메일 칸은 브라우저의 type=email 검사만 그대로 받는다).

import { AlertTriangle } from 'lucide-react';
import { useId, useState, type ComponentProps } from 'react';

import { HANGUL_WARNING, containsHangul } from '@/lib/korean-input';

export function HangulWarnedInput({ onInput, ...props }: ComponentProps<'input'>) {
  const warningId = useId();
  // 미리 채운 값(이전 단계에서 들고 온 이메일)도 같은 규칙으로 본다.
  const [hangul, setHangul] = useState(() => containsHangul(String(props.defaultValue ?? '')));

  return (
    <>
      <input
        {...props}
        aria-describedby={hangul ? warningId : props['aria-describedby']}
        onInput={(event) => {
          setHangul(containsHangul(event.currentTarget.value));
          onInput?.(event);
        }}
      />
      {hangul ? (
        <p id={warningId} role="status" className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle aria-hidden className="size-3.5 shrink-0 text-danger" />
          {HANGUL_WARNING}
        </p>
      ) : null}
    </>
  );
}
