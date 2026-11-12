'use client';

// GuardDuty를 켜지 못한 리전과 고객사에 보낼 안내(Post-MVP T-14 · #588 D). 연결 확인 결과가 GUARDDUTY_DISABLED일
// 때만 그린다. 켜는 것은 고객사가 자기 AWS 계정에서 하므로, 관리자는 리전별 할 일을 안내문으로 복사해 보낸다.
// 문구와 판정은 lib/guardduty.ts에 있다.

import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { GUARDDUTY_FEE_NOTICE, guardDutyFailures, guardDutyMessage, guardDutyReason } from '@/lib/guardduty';
import { GUARDDUTY_GUIDE_PATH } from '@/lib/return-to';
import type { TenantDetail } from '@/types/api';

export function GuardDutyGuidance({ tenant }: { tenant: TenantDetail }) {
  const verification = tenant.last_verification;
  if (verification?.result !== 'GUARDDUTY_DISABLED') return null;

  const failures = guardDutyFailures(verification);
  // 안내문에 넣을 절대 주소다. 복사할 때만 쓰는 값이라 화면에 그리지 않는다(서버 렌더에는 오리진이 없다).
  const origin = typeof window === 'undefined' ? '' : window.location.origin;
  const message = guardDutyMessage(tenant, failures, `${origin}${GUARDDUTY_GUIDE_PATH}`);

  return (
    <section aria-labelledby="guardduty-guidance" className="flex flex-col gap-3">
      <h3 id="guardduty-guidance" className="text-sm font-bold">GuardDuty를 켜지 못한 리전</h3>
      {failures.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {failures.map((failure) => {
            const reason = guardDutyReason(failure.reason_code);
            return (
              <li key={failure.region} className="bg-secondary rounded-md p-3 text-[13px]">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-mono font-semibold">{failure.region}</span>
                  <span>{reason.label}</span>
                  <code className="text-muted-foreground font-mono text-xs break-all">{failure.reason_code}</code>
                </p>
                <p className="text-foreground-secondary mt-1">{reason.action}</p>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-muted-foreground text-[13px]">
          이 확인 기록에는 리전 목록이 없습니다. 연결을 다시 확인하면 켜지 못한 리전이 나옵니다.
        </p>
      )}
      <p className="text-muted-foreground text-xs">{GUARDDUTY_FEE_NOTICE}</p>
      <div className="flex flex-wrap items-center gap-3">
        <CopyButton
          value={message}
          label="GuardDuty 안내문 복사"
          ariaLabel="고객사에 보낼 GuardDuty 안내문 복사"
          variant="outline"
          className="h-11 px-3.5 text-sm sm:h-9"
        />
        <Link
          href={GUARDDUTY_GUIDE_PATH}
          target="_blank"
          rel="noreferrer"
          className="text-primary text-[13px] hover:underline"
        >
          켜는 방법 안내 페이지
        </Link>
      </div>
    </section>
  );
}
