// 고객 상태 · 연결 확인 결과 배지와 입력 칸. 색은 EnumBadge가 소유한다(status-badge.tsx).

import { EnumBadge } from '@/components/status-badge';
import { TENANT_STATUS_LABELS, VERIFICATION_LABELS } from '@/lib/tenants';
import { formatKst } from '@/lib/utils';
import type { TenantStatus, TenantVerification } from '@/types/api';

export function TenantStatusBadge({ status }: { status: TenantStatus }) {
  return <EnumBadge entry={TENANT_STATUS_LABELS[status]} />;
}

/** 확인 기록이 없으면 "확인 전"이다. 결과는 그 시각의 관측값이라 시각을 같이 보여 준다. */
export function VerificationSummary({
  verification,
  align = 'end',
}: {
  verification: TenantVerification | null;
  /** 상세의 좌우 배치(Row)는 오른쪽, 목록 표 칸은 왼쪽에 붙인다. */
  align?: 'start' | 'end';
}) {
  if (verification === null) return <span className="text-muted-foreground text-xs">확인 전</span>;
  return (
    <span className={`inline-flex flex-wrap items-center gap-1.5 ${align === 'end' ? 'justify-end' : 'justify-start'}`}>
      <EnumBadge entry={VERIFICATION_LABELS[verification.result]} />
      <span className="text-muted-foreground text-xs">{formatKst(verification.verified_at)}</span>
    </span>
  );
}

export const INPUT =
  'h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 aria-invalid:border-destructive';

export function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-destructive text-xs">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-muted-foreground text-xs">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
