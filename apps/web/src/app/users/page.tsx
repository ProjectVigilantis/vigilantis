// 사용자 관리(#492) — 중앙 관리자 · 담당 고객이 배정된 고객 담당자 · 고객사 ADMIN 전용. GNB 메뉴는 그 계정에게만
// 보이지만, 실제 차단과 범위는 서버(403 `AUTH_FORBIDDEN` · 목록 필터)다. 주소를 직접 쳐서 들어온 고객사 OPERATOR ·
// VIEWER는 여기서 403 안내를 보고, 담당 고객이 없는 고객 담당자는 서버 응답(403 또는 빈 목록)에 따라 403 안내나
// 배정 없음 안내를 본다(components/users/users-view.tsx).

import type { Metadata } from 'next';
import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { UsersView } from '@/components/users/users-view';
import { ApiError } from '@/lib/api/client';
import { serverApi } from '@/lib/api/server';
import { USER_ADMIN_FORBIDDEN } from '@/lib/user-admin';

export const metadata: Metadata = { title: '사용자 관리 · Vigilantis' };

export default async function UsersPage() {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getUsers } = await serverApi();
  let users;
  try {
    users = await getUsers();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    if (error instanceof ApiError && error.code === 'AUTH_FORBIDDEN') {
      return (
        <div className="flex flex-col gap-4">
          <h1 className="text-lg font-semibold">사용자 관리</h1>
          <p role="alert" className="rounded-lg border p-4 text-sm text-muted-foreground">
            {USER_ADMIN_FORBIDDEN}
          </p>
        </div>
      );
    }
    return <ErrorState error={error} />;
  }
  return <UsersView users={users.items} />;
}
