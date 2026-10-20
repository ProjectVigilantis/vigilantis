'use client';

// 고객 추가 — 만들면 PENDING_CONNECTION이다. 연결(역할 생성 · 확인)은 상세 화면에서 이어 간다.

import { useRouter } from 'next/navigation';
import { useRef, useState } from 'react';

import { ErrorState } from '@/components/error-state';
import { Field, INPUT } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { createTenant } from '@/lib/api/client';
import { parseTenantForm, TENANT_NAME_MAX, type TenantForm, type TenantFormErrors } from '@/lib/tenants';

const EMPTY: TenantForm = {
  name: '',
  awsAccountId: '',
  accessRoleArn: '',
  regions: 'ap-northeast-2',
  collectVpcIds: '',
};

export function CreateTenantDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [form, setForm] = useState<TenantForm>(EMPTY);
  const [errors, setErrors] = useState<TenantFormErrors>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);

  const set = (key: keyof TenantForm) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((prev) => ({ ...prev, [key]: event.target.value }));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    const parsed = parseTenantForm(form);
    if (!parsed.ok) {
      setErrors(parsed.errors);
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
          <DialogTitle>고객 추가</DialogTitle>
          <DialogDescription>
            만든 뒤 상세 화면에서 고객에게 온보딩 링크를 보내고 연결을 확인합니다. 그 전에는 수집 · 분석 · 실행을 하지 않습니다.
          </DialogDescription>
        </DialogHeader>
        <form id="create-tenant" className="flex flex-col gap-4" onSubmit={submit} noValidate>
          <Field id="tenant-name" label="이름" error={errors.name}>
            <input id="tenant-name" className={INPUT} value={form.name} onChange={set('name')}
              maxLength={TENANT_NAME_MAX} disabled={pending} aria-invalid={Boolean(errors.name)}
              aria-describedby={describedBy('name', 'tenant-name')} autoFocus />
          </Field>
          <Field id="tenant-account" label="AWS 계정 ID" hint="숫자 12자리. 연결한 뒤에는 바꿀 수 없습니다." error={errors.awsAccountId}>
            <input id="tenant-account" className={`${INPUT} font-mono`} value={form.awsAccountId}
              onChange={set('awsAccountId')} inputMode="numeric" disabled={pending}
              aria-invalid={Boolean(errors.awsAccountId)} aria-describedby={describedBy('awsAccountId', 'tenant-account')} />
          </Field>
          <Field id="tenant-regions" label="리전" hint="여럿이면 쉼표나 공백으로 나눕니다." error={errors.regions}>
            <input id="tenant-regions" className={`${INPUT} font-mono`} value={form.regions}
              onChange={set('regions')} disabled={pending}
              aria-invalid={Boolean(errors.regions)} aria-describedby={describedBy('regions', 'tenant-regions')} />
          </Field>
          <Field id="tenant-vpcs" label="수집할 VPC(선택)" hint="비우면 계정 안 VPC 전체를 수집합니다." error={errors.collectVpcIds}>
            <input id="tenant-vpcs" className={`${INPUT} font-mono`} value={form.collectVpcIds}
              onChange={set('collectVpcIds')} placeholder="vpc-0123abcd" disabled={pending}
              aria-invalid={Boolean(errors.collectVpcIds)} aria-describedby={describedBy('collectVpcIds', 'tenant-vpcs')} />
          </Field>
          <Field id="tenant-role" label="역할 ARN(선택)" hint="비우면 온보딩 템플릿의 기본 역할 vigilantis-access를 씁니다." error={errors.accessRoleArn}>
            <input id="tenant-role" className={`${INPUT} font-mono`} value={form.accessRoleArn}
              onChange={set('accessRoleArn')} placeholder="arn:aws:iam::123456789012:role/vigilantis-access"
              disabled={pending} aria-invalid={Boolean(errors.accessRoleArn)}
              aria-describedby={describedBy('accessRoleArn', 'tenant-role')} />
          </Field>
        </form>
        {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>취소</Button>
          <Button type="submit" form="create-tenant" disabled={pending}>
            {pending ? '만드는 중…' : '고객 만들기'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
