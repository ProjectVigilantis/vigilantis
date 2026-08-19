// CMN-002 오류 상태 — 오류 봉투의 code 5종으로 분기하고 message를 그대로 보여줍니다(화면설계서 v1.4 §4.9).
// 주의: 이 파일에 "use client"를 붙이지 말 것 — RSC 직렬화가 ApiError의 code·requestId·httpStatus를
// 오류 없이 조용히 버려서(prod에선 message까지) 5종 분기가 전부 무너진다. 클라이언트 로직은 copy-button처럼 분리한다.

import { AlertTriangle } from 'lucide-react';
import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api/client';
import { cn } from '@/lib/utils';
import type { ErrorCode } from '@/types/api';

interface Presentation {
  /** 4.9 "화면 처리" 열 — page = 전체 오류 화면, inline = 해당 영역만. */
  variant: 'page' | 'inline';
  /** message 아래에 덧붙이는 안내. 없으면 message만 보여준다. */
  note?: string;
  backToList?: boolean;
}

/** Record라서 계약 오류 코드가 늘면 컴파일 에러가 난다. */
const PRESENTATION: Record<ErrorCode, Presentation> = {
  INCIDENT_NOT_FOUND: { variant: 'page', backToList: true },
  // 4.6 응답 처리 표의 문구 그대로
  IDEMPOTENCY_KEY_CONFLICT: {
    variant: 'inline',
    note: '요청이 변경되었습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.',
  },
  // 4.9: Toast + 재조회가 원칙 — Toast는 CMN-001 소관, "해당 Incident 재조회"는 이 컴포넌트가 아니라
  // 호출 화면 책임이다. 이 오류를 받는 화면은 반드시 재조회를 붙일 것 (화면설계서 4.6·5장 원칙 2).
  PROPOSAL_NOT_EXECUTABLE: { variant: 'inline' },
  // 4.9: "계약 불일치이므로 사용자 재시도로 해결되지 않음을 명시"
  REQUEST_VALIDATION_FAILED: {
    variant: 'inline',
    note: '계약 불일치 오류입니다. 다시 시도해도 해결되지 않습니다.',
  },
  INTERNAL_ERROR: { variant: 'page' },
};

/** ApiError가 아닌 실패(네트워크 등)도 오류 화면을 그려야 하므로 INTERNAL_ERROR로 접는다. */
function normalize(error: unknown): { code: ErrorCode; message: string; requestId: string } {
  if (error instanceof ApiError) {
    return { code: error.code, message: error.message, requestId: error.requestId };
  }
  return {
    code: 'INTERNAL_ERROR',
    message: error instanceof Error ? error.message : '요청이 실패했습니다',
    requestId: '',
  };
}

export function ErrorState({
  error,
  variant,
  className,
}: {
  error: unknown;
  /** 기본값은 code별 규칙(4.9). 모달·패널처럼 강제로 인라인이어야 하는 자리에서만 넘긴다. */
  variant?: 'page' | 'inline';
  className?: string;
}) {
  const { code, message, requestId } = normalize(error);
  const preset = PRESENTATION[code];
  const layout = variant ?? preset.variant;

  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm',
        layout === 'page' && 'items-center py-12 text-center',
        className,
      )}
    >
      <AlertTriangle aria-hidden className="size-4 text-destructive" />
      {/* error.message를 그대로 표시한다 — 화면이 문구를 다시 쓰지 않는다(4.9) */}
      <p className="font-medium text-foreground">{message}</p>
      {preset.note ? <p className="text-muted-foreground">{preset.note}</p> : null}

      {requestId ? (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">요청 ID</summary>
          <span className="mt-1 inline-flex items-center gap-1">
            <code className="font-mono">{requestId}</code>
            <CopyButton value={requestId} />
          </span>
        </details>
      ) : null}

      {preset.backToList ? (
        <Button asChild variant="outline" size="sm">
          <Link href="/incidents">목록으로</Link>
        </Button>
      ) : null}
    </div>
  );
}
