'use client';

// 고객 상세: 연결(온보딩 → 연결 확인) · 원격 회수(정지 · 재개) · 자동 조치 스위치(T-6) · 삭제.
//
// 상태 관문은 서버가 최종 판정한다. 그 사이 다른 관리자가 상태를 바꿨으면(TENANT_STATE_CONFLICT)
// 화면을 다시 읽는다. 낡은 화면의 버튼으로 다시 시도하지 않게 한다(PROPOSAL_NOT_EXECUTABLE과 같은 원칙).
//
// 쓰기 · 온보딩 자격 증명(링크 · ExternalId)은 이 고객을 다룰 수 있는 계정만 쓴다(#492 PR 5) — 중앙 관리자, 또는 이
// 고객이 배정된 고객 담당자(lib/roles.ts `canAccessTenant`). 확인 전 계정이나 배정 스냅샷에 없는 고객에는 버튼을 그리지
// 않고 조회만 보인다. 실제 차단은 서버(403 · 범위 밖 404)이고, 낡은 화면의 요청이 거절되면 오류를 보인 뒤 다시 읽는다.

import { ArrowLeft, ExternalLink, TriangleAlert } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useRef, useState } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { CopyButton } from '@/components/copy-button';
import { Row } from '@/components/detail-row';
import { ErrorState } from '@/components/error-state';
import { Panel } from '@/components/panel';
import { StatGrid, StatTile } from '@/components/stat-tile';
import { TenantStatusBadge, VerificationSummary } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import {
  ApiError,
  deleteTenant,
  getTenantOnboarding,
  resumeTenant,
  suspendTenant,
  updateTenant,
  verifyTenant,
} from '@/lib/api/client';
import type { LabelTone } from '@/lib/enum-labels';
import { canAccessTenant } from '@/lib/roles';
import {
  isStaleTenantRequest,
  SUSPEND_REASON_MAX,
  tenantActions,
  tenantRunFollowUp,
  VERIFICATION_LABELS,
  verificationHint,
  type TenantRunResult,
} from '@/lib/tenants';
import { formatKst } from '@/lib/utils';
import type { TenantDetail, TenantOnboardingResponse, TenantStatus, TenantVerification } from '@/types/api';

type Busy = 'verify' | 'resume' | 'suspend' | 'remove' | 'auto' | 'onboarding' | null;

export function TenantDetailView({ tenant }: { tenant: TenantDetail }) {
  const router = useRouter();
  const { session, refresh: refreshSession } = useSession();
  const canManage = session.kind === 'user' && canAccessTenant(session.user, tenant.tenant_id);
  const [busy, setBusy] = useState<Busy>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [onboarding, setOnboarding] = useState<TenantOnboardingResponse | null>(null);
  const [dialog, setDialog] = useState<'suspend' | 'remove' | null>(null);
  const inFlight = useRef(false);
  // 상태가 열어도 관리 권한이 없으면 동작을 그리지 않는다.
  const actions = canManage ? tenantActions(tenant.status) : { verify: false, suspend: false, resume: false, remove: false };
  const verification = tenant.last_verification;
  const hint = verification ? verificationHint(verification.result, tenant.status) : null;

  /**
   * 동작 하나를 실행한다. 성공하면 서버 값으로 다시 그린다. 그 사이 상태가 바뀐 실패(isStaleTenantRequest)도
   * 다시 그려 지금 가능한 동작을 보인다(tenantRunFollowUp). 다른 동작이 진행 중이면 건너뛰고 null을 돌려준다.
   */
  async function run(kind: Exclude<Busy, null>, call: () => Promise<unknown>): Promise<TenantRunResult | null> {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(kind);
    setFailure(null);
    let result: TenantRunResult;
    try {
      await call();
      result = { ok: true };
    } catch (error) {
      setFailure(error);
      result = { ok: false, code: error instanceof ApiError ? error.code : null };
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
    if (tenantRunFollowUp(result).refresh) router.refresh();
    // 403은 화면의 등급 · 배정 스냅샷이 낡았다는 뜻일 수 있다 — `/me`를 다시 읽어 버튼을 지금 권한에 맞춘다.
    // 401은 API 계층이 로그인으로 보내고, 재조회 실패는 이미 보인 오류 외에 더 알리지 않는다.
    if (!result.ok && result.code === 'AUTH_FORBIDDEN') refreshSession().catch(() => {});
    return result;
  }

  async function showOnboarding() {
    await run('onboarding', async () => setOnboarding(await getTenantOnboarding(tenant.tenant_id)));
  }

  const disabled = busy !== null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Link href="/tenants" className="text-muted-foreground inline-flex w-fit items-center gap-1 text-sm hover:underline">
          <ArrowLeft aria-hidden className="size-3.5" />
          고객 목록
        </Link>
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-2xl font-bold tracking-tight break-all">{tenant.name}</h1>
          <TenantStatusBadge status={tenant.status} />
          <span className="text-muted-foreground font-mono text-[13px]">{tenant.aws_account_id}</span>
        </div>
      </div>

      {tenant.status === 'SUSPENDED' ? (
        <div role="status" className="border-status-idle/50 bg-badge-idle-bg rounded-xl border p-4 text-sm">
          <p className="font-bold">원격 회수 중입니다. 이 고객 계정에서 새 수집 · 분석 · 실행을 하지 않습니다.</p>
          <p className="text-foreground-secondary mt-1">
            {formatKst(tenant.suspended_at)} 정지 · 사유: {tenant.suspended_reason}
          </p>
          <p className="text-muted-foreground mt-1 text-xs">
            지난 기록은 계속 볼 수 있습니다. 고객 계정의 역할은 고객 소유라 플랫폼이 지우지 않습니다.
          </p>
        </div>
      ) : verification !== null && verification.result !== 'OK' ? (
        <VerificationBanner verification={verification} status={tenant.status} />
      ) : null}

      {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}

      <StatGrid>
        <StatTile label="자산" value={tenant.asset_count} note={`리전 ${tenant.regions.length}곳`} />
        <StatTile label="열린 인시던트" value={tenant.open_incident_count} />
        <StatTile
          label="수집 범위"
          value={tenant.collect_vpc_ids.length === 0 ? '전체' : `VPC ${tenant.collect_vpc_ids.length}`}
          note={tenant.collect_vpc_ids.length === 0 ? '계정 안 VPC 전체' : '지정한 VPC만'}
        />
        <StatTile
          label="자동 조치"
          value={tenant.auto_action_enabled ? '켜짐' : '꺼짐'}
          note={tenant.auto_action_enabled ? 'High 선차단 · Medium 자동 격리' : '사람이 승인한 조치만 실행'}
        />
      </StatGrid>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="연결" description="플랫폼이 고객 계정의 역할을 ExternalId와 함께 맡아 들어갑니다. 장기 키는 받지 않습니다.">
          <div className="flex flex-col gap-3">
            <Stacked label="역할 ARN">
              <span className="font-mono text-xs">{tenant.access_role_arn}</span>
            </Stacked>
            <Row label="마지막 연결 확인">
              <VerificationSummary verification={verification} />
            </Row>
            {verification?.reason_code ? (
              <Stacked label="실패 코드">
                <code className="font-mono text-xs">{verification.reason_code}</code>
              </Stacked>
            ) : null}
            {hint ? <p className="text-muted-foreground text-xs">{hint}</p> : null}

            <div className="flex flex-wrap gap-2 pt-1">
              {actions.verify ? (
                <Button type="button" size="sm" disabled={disabled}
                  onClick={() => run('verify', () => verifyTenant(tenant.tenant_id))}>
                  {busy === 'verify' ? '확인 중…' : '연결 확인'}
                </Button>
              ) : null}
              {actions.resume ? (
                <Button type="button" size="sm" disabled={disabled}
                  onClick={() => run('resume', () => resumeTenant(tenant.tenant_id))}>
                  {busy === 'resume' ? '확인 중…' : '연결 확인 후 재개'}
                </Button>
              ) : null}
            </div>
            {actions.resume ? (
              <p className="text-muted-foreground text-xs">재개는 연결 확인이 먼저 성공해야 합니다. 실패하면 정지 상태 그대로입니다.</p>
            ) : null}
          </div>
        </Panel>

        <Panel title="온보딩" description="고객이 링크를 열면 고객 계정에 CloudFormation 스택으로 접근 역할이 만들어집니다.">
          <div className="flex flex-col gap-3 text-sm">
            {tenant.status === 'PENDING_CONNECTION' && tenant.onboarding_url ? (
              <ol className="text-muted-foreground list-decimal space-y-1 pl-5 text-xs">
                <li>아래 링크를 고객에게 보냅니다.</li>
                <li>고객이 자기 계정에서 스택을 만듭니다.</li>
                <li>[연결 확인]이 성공하면 관제를 시작합니다.</li>
              </ol>
            ) : (
              <p className="text-muted-foreground text-xs">
                고객이 역할을 지웠다면 같은 ExternalId로 역할을 다시 만들어야 다시 연결됩니다.
              </p>
            )}
            {!canManage ? (
              <p className="text-muted-foreground text-xs">
                온보딩 링크 · ExternalId는 중앙 관리자와 이 고객의 담당자만 볼 수 있습니다.
              </p>
            ) : onboarding === null ? (
              <Button type="button" size="sm" variant="outline" className="w-fit" disabled={disabled} onClick={showOnboarding}>
                {busy === 'onboarding' ? '불러오는 중…' : '온보딩 링크 · ExternalId 보기'}
              </Button>
            ) : (
              <div className="flex flex-col gap-2">
                <Row label="온보딩 링크">
                  <span className="inline-flex items-center gap-1">
                    <a href={onboarding.onboarding_url} target="_blank" rel="noreferrer"
                      className="inline-flex items-center gap-1 hover:underline">
                      열기 <ExternalLink aria-hidden className="size-3" />
                    </a>
                    <CopyButton value={onboarding.onboarding_url} label="링크 복사" />
                  </span>
                </Row>
                <Stacked label="ExternalId">
                  <span className="inline-flex flex-wrap items-center gap-1">
                    <code className="font-mono text-xs">{onboarding.external_id}</code>
                    <CopyButton value={onboarding.external_id} />
                  </span>
                </Stacked>
                <p className="text-muted-foreground text-xs">ExternalId는 이 고객 전용 비밀 값입니다. 고객 외에는 공유하지 마세요.</p>
              </div>
            )}
          </div>
        </Panel>

        <Panel title="관제 범위">
          <div className="flex flex-col gap-3">
            <Row label="AWS 계정"><span className="font-mono text-xs">{tenant.aws_account_id}</span></Row>
            <Row label="리전"><span className="font-mono text-xs">{tenant.regions.join(', ')}</span></Row>
            <Row label="수집 VPC">
              {tenant.collect_vpc_ids.length === 0
                ? <span className="text-muted-foreground text-xs">계정 안 VPC 전체</span>
                : <span className="font-mono text-xs break-all">{tenant.collect_vpc_ids.join(', ')}</span>}
            </Row>
            <Row label="등록">{formatKst(tenant.created_at)}</Row>
          </div>
        </Panel>

        <Panel title="자동 조치" description="끄면 자동 발동(High 선차단 · Medium 1분 무응답 자동 격리)만 하지 않습니다. 수집 · 분석 · 승인 실행은 그대로입니다.">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-sm">{tenant.auto_action_enabled ? '켜짐' : '꺼짐 · 사람이 승인한 조치만 실행'}</span>
            {canManage ? (
              <Button type="button" size="sm" variant="outline" disabled={disabled}
                onClick={() => run('auto', () => updateTenant(tenant.tenant_id, { auto_action_enabled: !tenant.auto_action_enabled }))}>
                {busy === 'auto' ? '바꾸는 중…' : tenant.auto_action_enabled ? '자동 조치 끄기' : '자동 조치 켜기'}
              </Button>
            ) : null}
          </div>
        </Panel>
      </div>

      {actions.suspend || actions.remove ? (
        <Panel title="위험 작업" className="ring-badge-critical-fg/30">
          <div className="flex flex-col gap-3 text-sm">
            {actions.suspend ? (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-muted-foreground text-xs">정지하면 플랫폼이 이 계정에 더 들어가지 않습니다(구독 정지 · 원격 회수).</p>
                <Button type="button" size="sm" variant="destructive" disabled={disabled} onClick={() => { setFailure(null); setDialog('suspend'); }}>
                  고객 정지
                </Button>
              </div>
            ) : null}
            {actions.remove ? (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-muted-foreground text-xs">연결된 적 없는 고객만 지울 수 있습니다. 계정 ID를 잘못 넣었을 때 씁니다.</p>
                <Button type="button" size="sm" variant="destructive" disabled={disabled} onClick={() => { setFailure(null); setDialog('remove'); }}>
                  고객 삭제
                </Button>
              </div>
            ) : null}
          </div>
        </Panel>
      ) : null}

      {dialog === 'suspend' ? (
        <SuspendDialog
          name={tenant.name}
          pending={busy === 'suspend'}
          error={failure}
          onClose={() => setDialog(null)}
          onSubmit={async (reason) => {
            const result = await run('suspend', () => suspendTenant(tenant.tenant_id, reason));
            // 성공했거나 그 사이 상태가 바뀌어 요청이 무효가 됐으면 닫는다. 오류는 화면 위쪽에 남고, 다시 읽은
            // 화면이 지금 가능한 동작(정지됐다면 재개)을 보인다. 일시 오류면 확인창 안에 오류를 보이고 남긴다.
            if (tenantRunFollowUp(result).closeDialog) setDialog(null);
          }}
        />
      ) : null}
      {dialog === 'remove' ? (
        <ConfirmRemoveDialog
          name={tenant.name}
          pending={busy === 'remove'}
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            if (inFlight.current) return;
            inFlight.current = true;
            setBusy('remove');
            setFailure(null);
            try {
              await deleteTenant(tenant.tenant_id);
              router.replace('/tenants');
            } catch (error) {
              setFailure(error);
              setDialog(null);
              if (isStaleTenantRequest(error instanceof ApiError ? error.code : null)) router.refresh();
              inFlight.current = false;
              setBusy(null);
            }
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * 마지막 연결 확인이 실패했을 때 맨 위에 다음 할 일을 보인다(DESIGN.md §경고 배너).
 * 확인 불가는 연결 여부와 무관해 대기 색, 권한 문제는 경고 색, 접근 거절 · 계정 불일치는 위험 색이다.
 */
const BANNER_TONE: Partial<Record<LabelTone, string>> = {
  red: 'border-badge-critical-fg/30 bg-badge-critical-bg text-badge-critical-fg',
  orange: 'border-badge-serious-fg/30 bg-badge-serious-bg text-badge-serious-fg',
  gray: 'border-status-idle/50 bg-badge-idle-bg text-badge-idle-fg',
};

function VerificationBanner({ verification, status }: { verification: TenantVerification; status: TenantStatus }) {
  const entry = VERIFICATION_LABELS[verification.result];
  const hint = verificationHint(verification.result, status);
  return (
    <div role="status" className={`flex gap-3 rounded-xl border p-4 ${BANNER_TONE[entry.tone] ?? BANNER_TONE.gray}`}>
      <TriangleAlert aria-hidden className="mt-0.5 size-5 shrink-0" />
      <div className="flex flex-col gap-1">
        <p className="font-bold">
          마지막 연결 확인: {entry.label} ({formatKst(verification.verified_at)})
        </p>
        {hint ? <p className="text-foreground-secondary text-[13px]">{hint}</p> : null}
      </div>
    </div>
  );
}

/** ARN · ExternalId처럼 긴 값은 라벨 아래 줄에 둔다. 좌우 배치(Row)는 값이 줄지 않아 칸 밖으로 넘친다. */
function Stacked({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <div className="min-w-0 break-all">{children}</div>
    </div>
  );
}

function SuspendDialog({
  name, pending, error, onClose, onSubmit,
}: {
  name: string;
  pending: boolean;
  /** 확인창이 열린 채 남는 실패(일시 오류)를 확인창 안에 보인다. 확인창이 페이지의 오류 표시를 가리기 때문이다. */
  error: unknown;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}) {
  const [reason, setReason] = useState('');
  const trimmed = reason.trim();
  return (
    <Dialog open onOpenChange={(next) => (next || pending ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{name} 정지</DialogTitle>
          <DialogDescription>
            새 수집 · 분석 · 실행을 멈춥니다. 이미 발급된 접근 세션은 만료될 때까지 남을 수 있습니다. 재개하려면 연결 확인이 다시 성공해야 합니다.
          </DialogDescription>
        </DialogHeader>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium">정지 사유(필수)</span>
          <textarea className="border-input bg-background min-h-20 rounded-md border p-2"
            value={reason} onChange={(event) => setReason(event.target.value)}
            maxLength={SUSPEND_REASON_MAX} disabled={pending} placeholder="예: 구독 만료" />
        </label>
        {error !== null ? <ErrorState error={error} variant="inline" /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="button" variant="destructive" disabled={pending || trimmed === ''} onClick={() => onSubmit(trimmed)}>
            {pending ? '정지하는 중…' : '정지'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ConfirmRemoveDialog({
  name, pending, onClose, onConfirm,
}: {
  name: string;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open onOpenChange={(next) => (next || pending ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{name} 삭제</DialogTitle>
          <DialogDescription>
            연결된 적이 없어 수집 · 인시던트 기록이 없습니다. 삭제하면 되돌릴 수 없고, 같은 계정은 새 고객으로 다시 추가합니다.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="button" variant="destructive" disabled={pending} onClick={onConfirm}>
            {pending ? '삭제하는 중…' : '삭제'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
