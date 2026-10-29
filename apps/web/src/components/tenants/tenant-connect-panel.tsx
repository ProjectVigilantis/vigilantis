'use client';

// 연결 대기 고객의 연결 패널. 위에서부터 고객에게 보낼 것, 고객 계정에 만들어질 것, 연결 확인 순서다.
// 실제 진행 순서와 같아서 이 순서로 두고, 번호는 달지 않는다. 앞의 두 일은 플랫폼이 관측하지 못해 끝났는지
// 표시할 수 없기 때문이다. 지금 무엇을 할지는 화면 맨 위 배너가 말한다(DESIGN.md §경고 배너).

import { CopyButton } from '@/components/copy-button';
import { Row } from '@/components/detail-row';
import { Panel } from '@/components/panel';
import { VerificationSummary } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import { ONBOARDING_STACK_NAME, onboardingMessage, roleNameOf } from '@/lib/tenants';
import type { TenantDetail } from '@/types/api';

/**
 * 좁은 화면에서는 손가락으로 누를 자리를 44px로 키우고, 넓은 화면에서는 DESIGN.md 버튼 높이(주 동작 40px ·
 * 보조 36px)로 돌아간다.
 */
const PRIMARY = 'h-11 px-4 text-sm sm:h-10';
const OUTLINE = 'h-11 px-3.5 text-sm sm:h-9';

export function TenantConnectPanel({
  tenant,
  onboardingUrl,
  verifying,
  disabled,
  onVerify,
  className,
}: {
  tenant: TenantDetail;
  /** 연결 대기 상세에는 늘 있다(tenants.py TenantDetail). */
  onboardingUrl: string;
  verifying: boolean;
  disabled: boolean;
  onVerify: () => void;
  className?: string;
}) {
  const verification = tenant.last_verification;
  const message = onboardingMessage(tenant, onboardingUrl);

  return (
    <Panel title="연결" className={className}>
      <div className="flex flex-col divide-y">
        <section aria-labelledby="connect-send" className="flex flex-col gap-3 pb-5">
          <h3 id="connect-send" className="text-sm font-bold">고객사에 보낼 것</h3>
          <p className="text-foreground-secondary text-[13px]">
            안내문에 연결 링크가 들어 있습니다. 링크 안에 이 고객사의 ExternalId가 있어서, 고객사가 따로 입력할 값은 없습니다.
          </p>
          <div className="flex flex-wrap gap-2">
            <CopyButton value={message} label="안내문 복사" ariaLabel="고객사 안내문 복사" variant="outline" className={OUTLINE} />
            <CopyButton value={onboardingUrl} label="링크만 복사" ariaLabel="연결 링크 복사" variant="outline" className={OUTLINE} />
          </div>
          <details className="group text-[13px]">
            <summary className="text-foreground-secondary hover:text-foreground focus-visible:ring-ring/50 inline-flex h-11 cursor-pointer items-center rounded-md px-1 focus-visible:ring-3 focus-visible:outline-none sm:h-8">
              안내문 보기
            </summary>
            <pre className="bg-secondary text-foreground-secondary mt-1 rounded-md p-3 font-sans break-all whitespace-pre-wrap">
              {message}
            </pre>
          </details>
        </section>

        <section aria-labelledby="connect-stack" className="flex flex-col gap-3 py-5">
          <h3 id="connect-stack" className="text-sm font-bold">고객사 AWS 계정에 만들어질 것</h3>
          {/* 패널이 화면 폭 전체라 라벨과 값이 양 끝으로 벌어지지 않게 묶음 폭을 줄인다. */}
          <div className="flex max-w-md flex-col gap-2">
            <Row label="스택"><span className="font-mono text-[13px] break-all">{ONBOARDING_STACK_NAME}</span></Row>
            <Row label="리전"><span className="font-mono text-[13px]">{tenant.regions[0]}</span></Row>
            <Row label="IAM 역할"><span className="font-mono text-[13px] break-all">{roleNameOf(tenant.access_role_arn)}</span></Row>
          </div>
        </section>

        <section aria-labelledby="connect-verify" className="flex flex-col gap-3 pt-5">
          <h3 id="connect-verify" className="text-sm font-bold">연결 확인</h3>
          <p className="text-foreground-secondary text-[13px]">
            플랫폼이 이 역할로 고객사 AWS 계정에 들어가 읽기 한 번을 해 봅니다. 성공하면 관제 중으로 바뀝니다.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" className={PRIMARY} disabled={disabled} onClick={onVerify}>
              {verifying ? '확인 중…' : '연결 확인'}
            </Button>
            <VerificationSummary verification={verification} align="start" />
          </div>
          {verification?.reason_code ? (
            <div className="max-w-md">
              <Row label="실패 코드"><code className="font-mono text-[13px] break-all">{verification.reason_code}</code></Row>
            </div>
          ) : null}
        </section>
      </div>
    </Panel>
  );
}
