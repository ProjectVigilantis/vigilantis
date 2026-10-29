// 클립보드 복사 버튼 — request_id를 모든 오류 화면에서 복사 가능하게 하려고 분리한 최소 클라이언트 조각입니다(화면설계서 v1.4 §4.9).

'use client';

import { Check, Copy } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';

export function CopyButton({
  value,
  label = '복사',
  ariaLabel,
  variant = 'ghost',
  className,
}: {
  value: string;
  label?: string;
  /** 값이 여러 줄로 길면(안내문 등) 값 대신 읽힐 이름을 준다. 기본은 `라벨: 값`이다. */
  ariaLabel?: string;
  /** 값 옆에 붙는 작은 버튼이 기본이다. 복사가 그 자리의 주 동작이면 테두리 버튼으로 키운다. */
  variant?: 'ghost' | 'outline';
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 클립보드 권한이 없으면 조용히 실패한다 — 값 자체는 화면에 그대로 보이므로 수동 복사가 가능하다
      setCopied(false);
    }
  }

  return (
    <Button type="button" variant={variant} size="xs" className={className} onClick={copy}
      aria-label={ariaLabel ?? `${label}: ${value}`}>
      {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
      {copied ? '복사됨' : label}
    </Button>
  );
}
