// 고객사 계정의 구독 화면 본체(T-16 · #528) — 지금 구독(투영) · 모의 Marketplace 계약 · 구매 요청 · 자동 갱신 끄기 ·
// 서비스 이용 중단.
//
// 값은 전부 /me/subscription 응답이다. 유료 권리의 원천은 서버 안의 모의 계약이고 실제 AWS 구매 · 청구가 없다.
// 구매 요청은 활성화 대기(PENDING)만 만든다 — 서버가 활성화하고 투영이 바뀐 응답을 읽기 전에는 구매 완료라고
// 말하지 않는다. 열 수 있는 쓰기는 서버의 `available_actions`다(화면 편의 · 실제 판정은 403 · 409).
//
// 쓰기는 서버 성공 응답으로만 화면을 바꾼다 — 확인창의 취소는 아무것도 보내지 않고, 실패하면 확인창과 입력을
// 남겨 다시 시도하게 한다. 그 사이 상태 · 권한이 바뀐 실패(isStaleSubscriptionRequest)는 확인창을 닫고 다시 읽는다.
//
// 훅을 쓰지만 `'use client'`를 붙이지 않는다 — 콜백 prop을 받으므로 클라이언트 경계(subscription-section.tsx)
// 안에서만 쓴다.

import { useRef, useState } from 'react';

import { Row } from '@/components/detail-row';
import { ErrorState } from '@/components/error-state';
import { Panel } from '@/components/panel';
import { EnumBadge } from '@/components/status-badge';
import { SubscriptionFacts } from '@/components/subscriptions/subscription-facts';
import { TenantStatusBadge } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { ApiError, cancelMyRenewal, purchaseMySubscription, stopMyService } from '@/lib/api/client';
import type { EnumLabel } from '@/lib/enum-labels';
import {
  AGREEMENT_STATUS_LABELS,
  agreementNote,
  AMOUNT_UNSET,
  can,
  changeNotice,
  isStaleSubscriptionRequest,
  manageNote,
  MOCK_MARKETPLACE_NOTICE,
  noticeSignature,
  PLAN_GUIDE,
  PROCESSING_NOT_GUARANTEED,
  PURCHASE_PENDING_RULE,
  RENEWAL_OFF,
  RENEWAL_RULE,
  RESUME_KEEPS_SUBSCRIPTION,
  STOP_KEEPS_CONTRACT,
  SUBSCRIPTION_POLL_MS,
  type SubscriptionChange,
} from '@/lib/subscriptions';
import { SUSPEND_REASON_MAX } from '@/lib/tenants';
import { formatKst } from '@/lib/utils';
import {
  SUBSCRIPTION_PURCHASE_PLANS,
  type CustomerSubscriptionResponse,
  type MockAgreement,
  type SubscriptionPurchasePlan,
} from '@/types/api';

const MOCK_BADGE: EnumLabel = { label: MOCK_MARKETPLACE_NOTICE, tone: 'blue' };

export function SubscriptionView({
  data,
  checkedAt,
  asOf,
  awaiting,
  refreshError,
  reloading,
  onChange,
  onReload,
  onRefresh,
  onHold,
}: {
  data: CustomerSubscriptionResponse;
  /** `data`를 받은 시각(ms). */
  checkedAt: number;
  /** 지난 권리 · 기다릴 것 판정의 기준 시각(ms) — 경계에서 다시 판정하면 `checkedAt`보다 뒤다. */
  asOf: number;
  /** 서버 처리를 기다리는 중이라 주기적으로 다시 읽는다(lib/subscriptions.ts `awaitingServer`). */
  awaiting: boolean;
  /** 마지막 다시 읽기의 실패 — 화면은 `data`(마지막으로 읽은 값) 그대로다. null = 없음. */
  refreshError: unknown;
  /** 요청 무효 뒤 다시 읽는 중 — `data`가 낡았으므로 새 쓰기를 열지 않는다. */
  reloading: boolean;
  /** 쓰기 성공 — 서버가 돌려준 바뀐 뒤의 응답. */
  onChange: (next: CustomerSubscriptionResponse) => void;
  /** 그 사이 상태 · 권한이 바뀌어 요청이 무효가 됐다 — 다시 읽는다. */
  onReload: () => void;
  /** 지금 다시 읽는다(쓰기를 막지 않는다). */
  onRefresh: () => void;
  /** 확인창이 열렸다 · 닫혔다 — 열린 동안 자동으로 다시 읽지 않는다. */
  onHold: (held: boolean) => void;
}) {
  const [dialog, setDialog] = useState<SubscriptionChange | null>(null);
  const [pending, setPending] = useState(false);
  /** 확인창이 열린 채 남는 실패. 확인창이 화면의 오류 표시를 가리므로 확인창 안에 보인다. */
  const [dialogError, setDialogError] = useState<unknown>(null);
  /** 확인창을 닫은 실패(요청 무효). */
  const [failure, setFailure] = useState<unknown>(null);
  /** 성공 알림과 그 알림이 가리키는 상태. 다시 읽은 응답의 상태가 바뀌면(활성화 · 실패 등) 내린다. */
  const [notice, setNotice] = useState<{ text: string; signature: string } | null>(null);
  /** 중복 제출의 동기 잠금 — state는 다음 렌더 전까지 버튼을 열어 둔다(action-execute-dialog.tsx와 같은 이유). */
  const inFlight = useRef(false);

  const note = manageNote(data);
  const stopped = data.service_status === 'SUSPENDED';
  const agreement = data.agreement;
  /** 쓰기 진행 중이거나 낡은 화면을 다시 읽는 중이면 버튼을 막는다. */
  const busy = pending || reloading;
  const shownNotice = notice !== null && notice.signature === noticeSignature(data) ? notice.text : null;

  function show(kind: SubscriptionChange | null) {
    setDialog(kind);
    onHold(kind !== null);
  }

  function open(kind: SubscriptionChange) {
    if (busy) return;
    setDialogError(null);
    setFailure(null);
    setNotice(null);
    show(kind);
  }

  function close() {
    if (inFlight.current) return;
    show(null);
    setDialogError(null);
  }

  async function submit(kind: SubscriptionChange, call: () => Promise<CustomerSubscriptionResponse>) {
    if (inFlight.current || reloading) return;
    inFlight.current = true;
    setPending(true);
    setDialogError(null);
    try {
      const next = await call();
      onChange(next);
      setNotice({ text: changeNotice(kind, next), signature: noticeSignature(next) });
      show(null);
    } catch (error) {
      if (isStaleSubscriptionRequest(error instanceof ApiError ? error.code : null)) {
        show(null);
        setFailure(error);
        onReload();
      } else {
        setDialogError(error);
      }
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-2xl font-bold tracking-tight">구독</h1>
          <TenantStatusBadge status={data.service_status} />
          <EnumBadge entry={MOCK_BADGE} />
        </div>
        <p className="text-muted-foreground text-sm break-all">
          {data.tenant_name} · 날짜 · 시각은 모두 KST입니다. 카드 정보를 받지 않고 실제 청구도 하지 않습니다.
        </p>
      </div>

      {stopped ? (
        <div role="status" className="border-status-idle/50 bg-badge-idle-bg rounded-xl border p-4 text-sm">
          <p className="font-bold">서비스 이용이 중단됐습니다. 새 수집 · 분석 · 조치를 하지 않습니다.</p>
          <p className="text-foreground-secondary mt-1">
            {STOP_KEEPS_CONTRACT}
            {can(data, 'CANCEL_RENEWAL') ? ' 중단 중에도 자동 갱신은 끌 수 있습니다.' : ''} 재개는 중앙 관리계정 또는
            해당 고객사가 배정된 관리자 직원계정에 요청하세요. {RESUME_KEEPS_SUBSCRIPTION}
          </p>
        </div>
      ) : null}

      {shownNotice !== null ? (
        <div role="status" className="border-badge-good-fg/30 bg-badge-good-bg text-badge-good-fg rounded-xl border p-4 text-sm font-semibold">
          {shownNotice}
        </div>
      ) : null}

      {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}
      {refreshError !== null ? (
        <div className="flex flex-col gap-1">
          <p className="text-muted-foreground text-xs">최신 상태를 다시 확인하지 못했습니다. 아래는 마지막으로 읽은 값입니다.</p>
          <ErrorState error={refreshError} variant="inline" />
        </div>
      ) : null}
      {reloading ? (
        <p role="status" className="text-muted-foreground text-sm">최신 구독 상태를 다시 불러오는 중입니다…</p>
      ) : null}
      {awaiting ? (
        <div role="status" className="bg-badge-warning-bg text-badge-warning-fg flex flex-wrap items-center justify-between gap-2 rounded-xl p-4 text-sm">
          <span>
            서버의 계약 처리 결과를 기다리는 중입니다. 이 화면은 {SUBSCRIPTION_POLL_MS / 1000}초마다 상태를 다시 읽을 뿐
            처리를 앞당기지 않습니다. {PROCESSING_NOT_GUARANTEED} · 마지막 확인{' '}
            {formatKst(new Date(checkedAt).toISOString())}
          </span>
          <Button type="button" size="sm" variant="outline" onClick={onRefresh}>지금 확인</Button>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="현재 구독" description="지금 적용 중인 요금제입니다. 서버가 계약을 반영한 값만 보입니다.">
          <div className="flex flex-col gap-3">
            <SubscriptionFacts
              subscription={data.subscription}
              source={data.source}
              entitlementEndsAt={data.entitlement_ends_at}
              asOf={asOf}
            />
            {data.next_renewal_date !== null ? (
              <Row label="다음 자동 갱신(예정)"><span className="tabular-nums">{data.next_renewal_date} 00시(KST)</span></Row>
            ) : null}
            {data.expiry_date !== null ? (
              <Row label="만료 · 앱 기본 Free(예정)"><span className="tabular-nums">{data.expiry_date} 00시(KST)</span></Row>
            ) : null}
            {can(data, 'PURCHASE') || can(data, 'CANCEL_RENEWAL') ? (
              <div className="flex flex-wrap gap-2 pt-1">
                {can(data, 'PURCHASE') ? (
                  <Button type="button" size="sm" disabled={busy} onClick={() => open('purchase')}>
                    {agreement?.status === 'FAILED' ? '다시 구매 요청' : '모의 Marketplace 구매 요청'}
                  </Button>
                ) : null}
                {can(data, 'CANCEL_RENEWAL') ? (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => open('cancel')}>
                    자동 갱신 끄기
                  </Button>
                ) : null}
              </div>
            ) : null}
            {note !== null ? <p className="text-muted-foreground text-xs">{note}</p> : null}
          </div>
        </Panel>

        <Panel title="모의 Marketplace 계약" description={MOCK_MARKETPLACE_NOTICE}>
          <AgreementFacts data={data} asOf={asOf} />
        </Panel>

        <Panel title="요금제 안내" description="요금 · 혜택 · SLA는 확정 전입니다.">
          <ul className="flex flex-col gap-3">
            {PLAN_GUIDE.map((guide) => (
              <li key={guide.plan} className="flex items-center justify-between gap-3 text-sm">
                <span className="font-semibold">{guide.plan}</span>
                <span className="text-muted-foreground text-xs">{guide.description}</span>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      {can(data, 'STOP') ? (
        <Panel title="위험 작업" className="ring-badge-critical-fg/30">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-muted-foreground text-xs">
              새 수집 · 분석 · 조치를 바로 멈춥니다. 구독 계약은 해지하지 않습니다. 재개는 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 요청하세요.
            </p>
            <Button type="button" size="sm" variant="destructive" disabled={busy} onClick={() => open('stop')}>
              서비스 이용 중단
            </Button>
          </div>
        </Panel>
      ) : null}

      {dialog === 'purchase' ? (
        <PurchaseDialog
          pending={pending}
          error={dialogError}
          onClose={close}
          onConfirm={(plan) => submit('purchase', () => purchaseMySubscription(plan))}
        />
      ) : null}
      {dialog === 'cancel' && agreement?.status === 'ACTIVE' ? (
        <CancelDialog
          agreement={agreement}
          pending={pending}
          error={dialogError}
          onClose={close}
          onConfirm={() => submit('cancel', () => cancelMyRenewal())}
        />
      ) : null}
      {dialog === 'stop' ? (
        <StopDialog
          pending={pending}
          error={dialogError}
          onClose={close}
          onConfirm={(reason) => submit('stop', () => stopMyService(reason))}
        />
      ) : null}
    </div>
  );
}

interface DialogProps {
  pending: boolean;
  error: unknown;
  onClose: () => void;
}

/**
 * 모의 계약의 서버 상태. 활성화 전(PENDING)에는 기간 · 권리가 없으므로 날짜를 만들지 않고 요청 시각만 보인다.
 * 실제 AWS 계약 · 링크 · ARN이 아니다.
 */
function AgreementFacts({ data, asOf }: { data: CustomerSubscriptionResponse; asOf: number }) {
  const agreement = data.agreement;
  if (agreement === null) {
    return (
      <p className="text-muted-foreground text-xs">
        모의 Marketplace 계약이 없습니다. 유료 요금제는 구매를 요청하고 서버가 활성화한 뒤에 적용됩니다.
      </p>
    );
  }
  const activated = agreement.status === 'ACTIVE' || agreement.status === 'EXPIRED';
  return (
    <div className="flex flex-col gap-3">
      <Row label="상태">
        <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
          <EnumBadge entry={AGREEMENT_STATUS_LABELS[agreement.status]} />
          {agreement.status === 'ACTIVE' && !agreement.auto_renew ? <EnumBadge entry={RENEWAL_OFF} /> : null}
        </span>
      </Row>
      <Row label="요금제"><span className="font-semibold">{agreement.plan}</span></Row>
      <Row label="구매 요청"><span className="tabular-nums">{formatKst(agreement.requested_at)}</span></Row>
      {activated ? (
        <>
          <Row label="활성화"><span className="tabular-nums">{formatKst(agreement.activated_at)}</span></Row>
          <Row label={agreement.status === 'EXPIRED' ? '마지막 기간' : '현재 기간'}>
            <span className="tabular-nums">{formatKst(agreement.term_start)} ~ {formatKst(agreement.term_end)}</span>
          </Row>
          <Row label="자동 갱신">{agreement.auto_renew ? '켜짐' : '꺼짐'}</Row>
        </>
      ) : null}
      {agreement.renewal_disabled_at !== null ? (
        <Row label="자동 갱신 끈 시각"><span className="tabular-nums">{formatKst(agreement.renewal_disabled_at)}</span></Row>
      ) : null}
      {agreement.ended_at !== null ? (
        <Row label={agreement.status === 'FAILED' ? '실패 시각' : '만료 시각'}>
          <span className="tabular-nums">{formatKst(agreement.ended_at)}</span>
        </Row>
      ) : null}
      {agreement.failure_code !== null ? (
        <Row label="실패 코드"><code className="font-mono text-xs">{agreement.failure_code}</code></Row>
      ) : null}
      <Row label="모의 계약 ID"><code className="font-mono text-xs break-all">{agreement.agreement_id}</code></Row>
      <p
        role={agreement.status === 'FAILED' ? 'alert' : undefined}
        className={agreement.status === 'FAILED' ? 'text-badge-critical-fg text-xs font-semibold' : 'text-muted-foreground text-xs'}
      >
        {agreementNote(data, asOf)}
      </p>
    </div>
  );
}

/** 모의 구매 요청 — 결제 화면 · 카드 입력 없이 확인창 하나다. 활성화 전이라 날짜 없이 계약 규칙만 보인다. */
function PurchaseDialog({ pending, error, onClose, onConfirm }: DialogProps & { onConfirm: (plan: SubscriptionPurchasePlan) => void }) {
  const [plan, setPlan] = useState<SubscriptionPurchasePlan | null>(null);
  return (
    <Dialog open onOpenChange={(next) => (next || pending ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>모의 Marketplace 구매 요청</DialogTitle>
          <DialogDescription>{MOCK_MARKETPLACE_NOTICE}. 카드 정보를 받지 않고 실제 청구도 하지 않습니다.</DialogDescription>
        </DialogHeader>
        <fieldset className="flex flex-col gap-2" disabled={pending}>
          <legend className="mb-1.5 text-sm font-medium">요금제</legend>
          {SUBSCRIPTION_PURCHASE_PLANS.map((option) => (
            <label key={option} className="has-checked:border-primary flex items-center gap-2 rounded-md border px-3 py-2 text-sm">
              <input type="radio" name="subscription-plan" value={option}
                checked={plan === option} onChange={() => setPlan(option)} />
              {option}
            </label>
          ))}
        </fieldset>
        <div className="flex flex-col gap-2">
          <Row label="요금">{AMOUNT_UNSET}</Row>
          <Row label="계약">월 단위 · 자동 갱신</Row>
        </div>
        <p className="text-muted-foreground text-xs">{RENEWAL_RULE}</p>
        <p className="text-xs font-semibold">{PURCHASE_PENDING_RULE}</p>
        {error !== null ? <ErrorState error={error} variant="inline" /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="button" disabled={pending || plan === null} onClick={() => (plan === null ? undefined : onConfirm(plan))}>
            {pending ? '요청하는 중…' : '구매 요청'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 자동 갱신 끄기 — 즉시 해지가 아니다. 끝 시각은 서버의 계약 기간 끝(정확한 시각)이다. */
function CancelDialog({
  agreement, pending, error, onClose, onConfirm,
}: DialogProps & { agreement: MockAgreement; onConfirm: () => void }) {
  return (
    <Dialog open onOpenChange={(next) => (next || pending ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>자동 갱신 끄기</DialogTitle>
          <DialogDescription>
            즉시 해지가 아닙니다. 지금 기간 끝 시각 {formatKst(agreement.term_end)}까지 {agreement.plan} 권리가 유지되고,
            그 뒤 서버가 만료를 처리하면 앱 기본 Free로 돌아갑니다.
          </DialogDescription>
        </DialogHeader>
        <p className="text-muted-foreground text-xs">
          이 화면에서 자동 갱신을 다시 켤 수 없습니다. Free로 돌아가도 AWS 연결을 끊거나 기록을 지우지 않습니다.{' '}
          {MOCK_MARKETPLACE_NOTICE}.
        </p>
        {error !== null ? <ErrorState error={error} variant="inline" /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="button" disabled={pending} onClick={onConfirm}>
            {pending ? '끄는 중…' : '자동 갱신 끄기'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 서비스 이용 중단 — 계약과 다른 확인창이다. 고객사 관리 화면의 정지와 같은 규칙 · 같은 사유 칸이다. 계약을 해지하지 않는다. */
function StopDialog({ pending, error, onClose, onConfirm }: DialogProps & { onConfirm: (reason: string) => void }) {
  const [reason, setReason] = useState('');
  const trimmed = reason.trim();
  return (
    <Dialog open onOpenChange={(next) => (next || pending ? undefined : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>서비스 이용 중단</DialogTitle>
          <DialogDescription>
            새 수집 · 분석 · 조치를 바로 멈춥니다. 이미 시작된 실행의 마무리와 자동 원복은 끝까지 진행합니다.
          </DialogDescription>
        </DialogHeader>
        <p className="text-muted-foreground text-xs">
          {STOP_KEEPS_CONTRACT} 자동 갱신을 끄려면 [자동 갱신 끄기]를 따로 요청하세요. 재개는 중앙 관리계정 또는
          해당 고객사가 배정된 관리자 직원계정에 요청하세요. {RESUME_KEEPS_SUBSCRIPTION}
        </p>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium">중단 사유(필수)</span>
          <textarea className="border-input bg-background min-h-20 rounded-md border p-2"
            value={reason} onChange={(event) => setReason(event.target.value)}
            maxLength={SUSPEND_REASON_MAX} disabled={pending} />
        </label>
        {error !== null ? <ErrorState error={error} variant="inline" /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="button" variant="destructive" disabled={pending || trimmed === ''} onClick={() => onConfirm(trimmed)}>
            {pending ? '중단하는 중…' : '이용 중단'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
