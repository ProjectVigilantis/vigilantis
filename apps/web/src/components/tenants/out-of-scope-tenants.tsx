'use client';

// 담당 밖 고객사 검색(#572 7번). 관리자 직원계정의 고객사 목록은 담당 그룹 고객사뿐이라, 담당 밖 고객사는 서버 검색
// (`GET /tenants/search`, 이름 부분 일치 · 최대 20건)으로만 찾는다. 담당 밖 결과는 권한 없음으로 보이고 상세로 잇지 않는다
// (서버도 403이다). 담당 안 결과는 위 목록 표가 이미 보인다. 입력이 멈춘 뒤 300ms에 묻는다.

import { useEffect, useState } from 'react';

import { searchTenants } from '@/lib/api/client';
import { outOfScopeResults, tenantSearchQuery } from '@/lib/tenant-groups';
import type { TenantSearchItem } from '@/types/api';

type Result = { q: string; items: TenantSearchItem[] } | { q: string; failed: true };

export function OutOfScopeTenants({ query }: { query: string }) {
  const q = tenantSearchQuery(query);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    if (q === null) return;
    let alive = true;
    const timer = setTimeout(() => {
      searchTenants(q)
        .then((res) => alive && setResult({ q, items: outOfScopeResults(res.items) }))
        .catch(() => alive && setResult({ q, failed: true }));
    }, 300);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [q]);

  // 지난 검색어의 결과는 그리지 않는다.
  if (q === null || result === null || result.q !== q) return null;
  if ('failed' in result) {
    return (
      <p role="alert" className="text-destructive border-t px-6 py-3 text-sm">
        담당 밖 고객사를 검색하지 못했습니다.
      </p>
    );
  }
  if (result.items.length === 0) return null;
  return (
    <div className="border-t px-6 py-4">
      <h3 className="text-sm font-semibold">담당 밖 고객사 {result.items.length}곳</h3>
      <ul className="mt-2 flex flex-col gap-1.5">
        {result.items.map((item) => (
          <li key={item.tenant_id} className="flex items-center gap-2 text-sm">
            <span>{item.name}</span>
            <span className="bg-muted text-muted-foreground rounded-full px-2 py-0.5 text-xs">권한 없음</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
