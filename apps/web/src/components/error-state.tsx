// CMN-002 오류 상태 — 오류 봉투의 code 9종으로 분기하고 message를 그대로 보여줍니다(화면설계서 v1.4 §4.9).
// 주의: 이 파일에 "use client"를 붙이지 말 것 — RSC 직렬화가 ApiError의 code·requestId·httpStatus를
// 오류 없이 조용히 버려서(prod에선 message까지) 5종 분기가 전부 무너진다. 클라이언트 로직은 copy-button처럼 분리한다.

import { AlertTriangle } from 'lucide-react';
import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api/client';
import { VIEWER_ACTION_BLOCKED } from '@/lib/roles';
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
  // 종료 처리(#199)가 받는 409 — 그 사이 상태가 옮겨졌다는 뜻이라 PROPOSAL_NOT_EXECUTABLE과
  // 같은 처리다: 모달을 닫고 상세를 재조회한다(재조회는 호출 화면 책임).
  INCIDENT_NOT_RESOLVABLE: { variant: 'inline' },
  // 4.9: "계약 불일치이므로 사용자 재시도로 해결되지 않음을 명시"
  REQUEST_VALIDATION_FAILED: {
    variant: 'inline',
    note: '계약 불일치 오류입니다. 다시 시도해도 해결되지 않습니다.',
  },
  INTERNAL_ERROR: { variant: 'page' },
  // 고객(테넌트) 관리 API(Post-MVP T-4). 화면 문구는 관리자 대시보드를 붙일 때 FE 담당이 다시 정한다.
  TENANT_NOT_FOUND: { variant: 'page', backToList: true },
  TENANT_ACCOUNT_CONFLICT: { variant: 'inline', note: '이 AWS 계정은 이미 다른 고객으로 등록돼 있습니다.' },
  TENANT_ONBOARDING_UNAVAILABLE: {
    variant: 'inline',
    note: '온보딩 링크를 만들 서버 설정이 없습니다. 플랫폼 운영자에게 알려 주세요.',
  },
  // 그 사이 다른 관리자가 상태를 바꿨다는 뜻 — PROPOSAL_NOT_EXECUTABLE 처럼 재조회가 원칙이다.
  TENANT_STATE_CONFLICT: {
    variant: 'inline',
    note: '그 사이 고객 상태가 바뀌었습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.',
  },
  TENANT_SUSPENDED: { variant: 'inline', note: '정지된 고객입니다. 고객을 재개한 뒤 실행할 수 있습니다.' },
  // 인증(#492). 로그인 화면은 자기 문구(lib/auth-errors.ts)를 쓰고, 여기는 일반 화면에 섞여 오는 경우다.
  // 미인증은 보통 여기까지 오지 않는다 — 서버 조회는 redirect, 브라우저 조회는 로그인 이동이 먼저 걸린다(PR 2).
  AUTH_REQUIRED: { variant: 'page', note: '로그인한 뒤 다시 시도해 주세요.' },
  // 역할이 막은 조치(VIEWER의 실행 · 종료). 다시 눌러도 같은 결과다.
  AUTH_FORBIDDEN: { variant: 'inline', note: VIEWER_ACTION_BLOCKED },
  AUTH_FAILED: { variant: 'inline' },
  AUTH_CODE_INVALID: { variant: 'inline' },
  AUTH_FLOW_EXPIRED: { variant: 'inline' },
  AUTH_STEP_CONFLICT: { variant: 'inline' },
  AUTH_PASSWORD_POLICY: { variant: 'inline' },
  AUTH_ORIGIN_REJECTED: { variant: 'inline' },
  ACCOUNT_NOT_PROVISIONED: { variant: 'inline' },
  ACCOUNT_DISABLED: { variant: 'inline' },
  AUTH_EMAIL_DELIVERY_FAILED: { variant: 'inline' },
  AUTH_RATE_LIMITED: { variant: 'inline' },
  AUTH_PROVIDER_UNAVAILABLE: { variant: 'inline' },
  // 사용자 관리(#492 PR 3). 화면(components/users/users-view.tsx)은 자기 문구(lib/user-admin.ts)를 쓰고,
  // 여기는 일반 화면에 섞여 오는 경우다. 그 사이 다른 관리자가 바꿨을 수 있어 재조회가 원칙이다.
  USER_NOT_FOUND: { variant: 'inline', note: '화면을 새로고침한 뒤 다시 확인해 주세요.' },
  USER_STATE_CONFLICT: { variant: 'inline', note: '화면을 새로고침한 뒤 다시 확인해 주세요.' },
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
        'flex flex-col gap-2 rounded-lg border border-danger/30 bg-danger/5 p-4 text-sm',
        layout === 'page' && 'items-center py-12 text-center',
        className,
      )}
    >
      <AlertTriangle aria-hidden className="size-4 text-danger" />
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

      {/* `대시보드로`는 **전체 오류 화면의 탈출구**다 — 화면이 통째로 죽어 갈 곳이 없을 때만 의미가 있다.
          인라인 오류는 주변 화면이 살아 있으므로 붙이지 않는다. 목록 안에서 카드 1건이 실패했는데
          이 버튼이 뜨면 이미 있는 목록으로 보내면서 필터만 푼다(PR #180 리뷰).
          목적지가 대시보드인 이유(2026-09-28): 인시던트 목록이 자산·보안 관제의 탭으로 흡수돼, 카테고리를
          모르는 이 자리에서는 어느 탭으로 보낼지 고를 수 없다. 대시보드의 띠 두 묶음이 양쪽으로 잇는다. */}
      {preset.backToList && layout === 'page' ? (
        <Button asChild variant="outline" size="sm">
          <Link href="/">대시보드로</Link>
        </Button>
      ) : null}
    </div>
  );
}
