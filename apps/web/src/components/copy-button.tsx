// 클립보드 복사 버튼 — request_id를 모든 오류 화면에서 복사 가능하게 하려고 분리한 최소 클라이언트 조각입니다(화면설계서 v1.4 §4.9).

'use client';

import { Check, Copy } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';

export function CopyButton({ value, label = '복사' }: { value: string; label?: string }) {
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
    <Button type="button" variant="ghost" size="xs" onClick={copy} aria-label={`${label}: ${value}`}>
      {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
      {copied ? '복사됨' : label}
    </Button>
  );
}
