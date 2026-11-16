'use client';

// 고객사 상세의 담당 그룹 칸(#572 2번). 중앙 관리계정만 그룹에 넣고 빼고(`PUT /tenants/{id}/group`), 그 밖의 계정은 이름만 본다.
// 이름은 `GET /groups`에서 찾는다. 관리자 직원계정은 자기 담당 그룹만 받지만, 볼 수 있는 고객사는 그 그룹 안에 있어 이름이 나온다.
// 저장 뒤에는 상세를 다시 읽는다. 부르는 쪽이 `key`에 지금 그룹을 넣어 선택값이 서버 값으로 돌아오게 한다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { INPUT_CLASS } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import { ApiError, listGroups, setTenantGroup } from '@/lib/api/client';
import { isCentralAdmin } from '@/lib/roles';
import { cn } from '@/lib/utils';
import type { GroupItem } from '@/types/api';

export function TenantGroupField({ tenantId, groupId }: { tenantId: string; groupId: string | null }) {
  const router = useRouter();
  const { session } = useSession();
  const central = session.kind === 'user' && isCentralAdmin(session.user);
  const [groups, setGroups] = useState<GroupItem[] | 'error' | null>(null);
  const [selected, setSelected] = useState(groupId ?? '');
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);

  useEffect(() => {
    let alive = true;
    listGroups()
      .then((res) => alive && setGroups(res.items))
      .catch(() => alive && setGroups('error'));
    return () => {
      alive = false;
    };
  }, []);

  if (groups === null) return <span className="text-muted-foreground text-xs">불러오는 중…</span>;
  if (groups === 'error') return <span className="text-muted-foreground text-xs">그룹을 불러오지 못했습니다.</span>;

  const current = groupId === null ? null : (groups.find((g) => g.group_id === groupId)?.name ?? groupId);
  if (!central) return current === null ? <span className="text-muted-foreground">그룹 없음</span> : <span>{current}</span>;

  async function save() {
    setSaving(true);
    setFailure(null);
    try {
      await setTenantGroup(tenantId, selected === '' ? null : selected);
    } catch (error) {
      setFailure(error);
    } finally {
      setSaving(false);
    }
    // 실패해도 다시 읽는다. 그 사이 그룹이 지워졌으면(GROUP_NOT_FOUND) 지금 그룹을 보여야 한다.
    router.refresh();
  }

  return (
    <span className="flex flex-col items-end gap-1.5">
      <span className="flex items-center gap-2">
        <label htmlFor="tenant-group" className="sr-only">
          담당 그룹
        </label>
        <select
          id="tenant-group"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          disabled={saving}
          className={cn(INPUT_CLASS, 'h-8 w-48')}
        >
          <option value="">그룹 없음</option>
          {groups.map((g) => (
            <option key={g.group_id} value={g.group_id}>
              {g.name}
            </option>
          ))}
        </select>
        <Button type="button" size="sm" disabled={saving || selected === (groupId ?? '')} onClick={save}>
          {saving ? '저장하는 중…' : '저장'}
        </Button>
      </span>
      {groups.length === 0 ? (
        <Link href="/tenants/groups" className="text-xs hover:underline">
          그룹 만들기
        </Link>
      ) : null}
      {/* `Row`가 값을 span으로 감싸므로 블록 요소(ErrorState)를 넣지 않고 서버 문구만 보인다. */}
      {failure !== null ? (
        <span role="alert" className="text-destructive text-xs">
          {failure instanceof ApiError ? failure.message : '담당 그룹을 바꾸지 못했습니다.'}
        </span>
      ) : null}
    </span>
  );
}
