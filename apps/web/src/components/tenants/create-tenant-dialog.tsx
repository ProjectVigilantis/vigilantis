'use client';

// 고객 추가: 만들면 PENDING_CONNECTION이다. 연결(역할 생성 · 확인)은 상세 화면에서 이어 간다.

import { useRouter } from 'next/navigation';
import { useRef, useState } from 'react';

import { ErrorState } from '@/components/error-state';
import { Field, INPUT } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { createTenant } from '@/lib/api/client';
import {
  DEFAULT_ACCESS_ROLE_NAME,
  parseTenantForm,
  TENANT_FORM_ADVANCED,
  TENANT_NAME_MAX,
  type TenantForm,
  type TenantFormErrors,
} from '@/lib/tenants';

const EMPTY: TenantForm = {
  name: '',
  awsAccountId: '',
  roleName: '',
  regions: 'ap-northeast-2',
  collectVpcIds: '',
};

export function CreateTenantDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [form, setForm] = useState<TenantForm>(EMPTY);
  const [errors, setErrors] = useState<TenantFormErrors>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const inFlight = useRef(false);

  const set = (key: keyof TenantForm) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((prev) => ({ ...prev, [key]: event.target.value }));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    const parsed = parseTenantForm(form);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      if (TENANT_FORM_ADVANCED.some((key) => parsed.errors[key] !== undefined)) setAdvanced(true);
      return;
    }
    setErrors({});
    setFailure(null);
    inFlight.current = true;
    setPending(true);
    try {
      const created = await createTenant(parsed.request);
      router.push(`/tenants/${encodeURIComponent(created.tenant.tenant_id)}`);
    } catch (error) {
      setFailure(error);
      inFlight.current = false;
      setPending(false);
    }
  }

  const describedBy = (key: keyof TenantForm, id: string) =>
    errors[key] ? `${id}-error` : `${id}-hint`;

  return (
    <Dialog open onOpenChange={(next) => (next || inFlight.current ? undefined : onClose())}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>고객사 추가</DialogTitle>
          <DialogDescription>
            만든 뒤 상세 화면에서 고객사에 온보딩 링크를 보내고 연결을 확인합니다. 그 전에는 수집 · 분석 · 실행을 하지 않습니다.
          </DialogDescription>
        </DialogHeader>
        <form id="create-tenant" className="flex flex-col gap-4" onSubmit={submit} noValidate>
          <Field id="tenant-name" label="이름" error={errors.name}>
            <input id="tenant-name" className={INPUT} value={form.name} onChange={set('name')}
              maxLength={TENANT_NAME_MAX} disabled={pending} aria-invalid={Boolean(errors.name)}
              aria-describedby={describedBy('name', 'tenant-name')} autoFocus />
          </Field>
          <Field
            id="tenant-account"
            label="AWS 계정 ID"
            hint="고객사 AWS 콘솔 오른쪽 위 계정 메뉴의 12자리 숫자입니다. 하이픈째 붙여 넣어도 됩니다. 추가한 뒤에는 바꿀 수 없습니다."
            error={errors.awsAccountId}
          >
            <input id="tenant-account" className={`${INPUT} font-mono`} value={form.awsAccountId}
              onChange={set('awsAccountId')} inputMode="numeric" autoComplete="off" disabled={pending}
              aria-invalid={Boolean(errors.awsAccountId)} aria-describedby={describedBy('awsAccountId', 'tenant-account')} />
          </Field>

          {/* 대부분의 고객은 기본값 그대로 쓴다. 필수 칸만 먼저 보이게 접어 두고, 이 안에 오류가 나면 펼친다. */}
          <details className="rounded-md border" open={advanced}
            onToggle={(event) => setAdvanced(event.currentTarget.open)}>
            <summary className="text-foreground-secondary hover:text-foreground focus-visible:ring-ring/50 flex h-11 cursor-pointer items-center rounded-md px-3 text-sm font-medium focus-visible:ring-3 focus-visible:outline-none sm:h-9">
              추가 설정
            </summary>
            <div className="flex flex-col gap-4 border-t p-3">
              <Field id="tenant-regions" label="리전" hint="여럿이면 쉼표나 공백으로 나눕니다. 연결 링크의 스택은 첫 리전에 만들어집니다." error={errors.regions}>
                <input id="tenant-regions" className={`${INPUT} font-mono`} value={form.regions}
                  onChange={set('regions')} disabled={pending}
                  aria-invalid={Boolean(errors.regions)} aria-describedby={describedBy('regions', 'tenant-regions')} />
              </Field>
              <Field id="tenant-vpcs" label="수집할 VPC" hint="비우면 계정 안 VPC 전체를 수집합니다." error={errors.collectVpcIds}>
                <input id="tenant-vpcs" className={`${INPUT} font-mono`} value={form.collectVpcIds}
                  onChange={set('collectVpcIds')} placeholder="vpc-0123abcd" disabled={pending}
                  aria-invalid={Boolean(errors.collectVpcIds)} aria-describedby={describedBy('collectVpcIds', 'tenant-vpcs')} />
              </Field>
              <Field
                id="tenant-role"
                label="역할 이름"
                hint={`고객사가 역할 이름 규칙 때문에 다른 이름을 써야 할 때만 넣습니다. 비우면 ${DEFAULT_ACCESS_ROLE_NAME}입니다.`}
                error={errors.roleName}
              >
                <input id="tenant-role" className={`${INPUT} font-mono`} value={form.roleName}
                  onChange={set('roleName')} placeholder={DEFAULT_ACCESS_ROLE_NAME} autoComplete="off"
                  disabled={pending} aria-invalid={Boolean(errors.roleName)}
                  aria-describedby={describedBy('roleName', 'tenant-role')} />
              </Field>
            </div>
          </details>
        </form>
        {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}
        <DialogFooter>
          {/* 좁은 화면은 누를 자리 44px, 넓은 화면은 DESIGN.md 버튼 높이(보조 36px · 주 동작 40px) */}
          <Button type="button" variant="outline" className="h-11 px-3.5 sm:h-9" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="submit" form="create-tenant" className="h-11 px-4 sm:h-10" disabled={pending}>
            {pending ? '만드는 중…' : '고객사 만들기'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
