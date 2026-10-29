// 네트워크 실패 회귀 — `npm test`. BE 미기동 시 조회 화면 전부가 멈추던 원인(React Flight dev 직렬화)을 고정합니다.

import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { test } from 'node:test';

import {
  ApiError,
  changeUserRole,
  changeUserStatus,
  confirmPasswordReset,
  createReadApi,
  getAssets,
  getMe,
  getUserEvents,
  inviteUser,
  requestPasswordReset,
  resendInvite,
  revokeUserSessions,
} from './client.ts';
import type { UserListItem } from '../../types/api.ts';

interface Sent {
  url: string;
  method: string;
  body: unknown;
}

/** fetch를 바꿔 끼워 보낸 요청을 기록하고 정해 둔 응답을 돌려준다. 테스트가 끝나면 되돌린다. */
function stubFetch(t: { after: (fn: () => void) => void }, respond: () => Response): Sent[] {
  const realFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = realFetch;
  });
  const sent: Sent[] = [];
  globalThis.fetch = async (input, init) => {
    sent.push({
      url: String(input),
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
    });
    return respond();
  };
  return sent;
}

const noContent = () => new Response(null, { status: 204 });
const errorResponse = (status: number, code: string) => () =>
  new Response(JSON.stringify({ error: { code, message: 'm', request_id: 'r' } }), { status });

test('비밀번호 재설정 요청 · 확인은 입력값을 다듬지 않고 그대로 보낸다', async (t) => {
  const sent = stubFetch(t, noContent);
  await requestPasswordReset(' User@Example.com ');
  await confirmPasswordReset('User@Example.com', '012345', ' Aa1!aaaaaaaa ');
  assert.match(sent[0].url, /\/api\/v1\/auth\/password-reset$/);
  assert.equal(sent[0].method, 'POST');
  assert.deepEqual(sent[0].body, { email: ' User@Example.com ' });
  assert.match(sent[1].url, /\/api\/v1\/auth\/password-reset\/confirm$/);
  assert.deepEqual(sent[1].body, { email: 'User@Example.com', code: '012345', new_password: ' Aa1!aaaaaaaa ' });
});

test('사용자 관리 조치의 경로 · 메서드 · 본문', async (t) => {
  const sent = stubFetch(t, noContent);
  await resendInvite('u/1');
  await changeUserRole('u/1', 'OPERATOR');
  await changeUserStatus('u/1', 'DISABLED');
  await revokeUserSessions('u/1');
  const path = (s: Sent) => new URL(s.url).pathname + new URL(s.url).search;
  assert.deepEqual(
    sent.map((s) => [s.method, path(s), s.body]),
    [
      ['POST', '/api/v1/users/u%2F1/invite/resend', {}],
      ['PUT', '/api/v1/users/u%2F1/role', { role: 'OPERATOR' }],
      ['PUT', '/api/v1/users/u%2F1/status', { status: 'DISABLED' }],
      ['POST', '/api/v1/users/u%2F1/sessions/revoke', {}],
    ],
  );
});

test('초대는 고객 ID · 복구 선택을 그대로 싣고 만든 고객사 사용자를 돌려준다', async (t) => {
  const tenantId = '123e4567-e89b-12d3-a456-426614174000';
  const created: UserListItem = {
    account_type: 'TENANT',
    tenant_id: tenantId,
    user_id: 'u1',
    email: 'a@b.c',
    role: 'VIEWER',
    status: 'ACTIVE',
    last_login_at: null,
  };
  const sent = stubFetch(t, () => new Response(JSON.stringify(created), { status: 201 }));
  const result = await inviteUser({ email: 'a@b.c', role: 'VIEWER', tenant_id: tenantId, recover_existing: false });
  assert.deepEqual(result, created);
  assert.equal(sent[0].method, 'POST');
  assert.match(sent[0].url, /\/api\/v1\/users$/);
  assert.deepEqual(sent[0].body, { email: 'a@b.c', role: 'VIEWER', tenant_id: tenantId, recover_existing: false });
});

test('고객사 ADMIN의 초대는 tenant_id 없이 나간다', async (t) => {
  const sent = stubFetch(t, () => new Response('{}', { status: 201 }));
  await inviteUser({ email: 'a@b.c', role: 'OPERATOR', recover_existing: false });
  assert.deepEqual(sent[0].body, { email: 'a@b.c', role: 'OPERATOR', recover_existing: false });
});

test('이력 조회는 상한 50을 싣는다', async (t) => {
  const sent = stubFetch(t, () => new Response(JSON.stringify({ items: [] }), { status: 200 }));
  assert.deepEqual(await getUserEvents('u1'), { items: [] });
  assert.equal(sent[0].method, 'GET');
  assert.match(sent[0].url, /\/api\/v1\/users\/u1\/events\?limit=50$/);
});

test('사용자 목록 — 401만 로그인 처리를 부르고, 403은 오류로만 올린다', async (t) => {
  let respond = errorResponse(401, 'AUTH_REQUIRED');
  stubFetch(t, () => respond());
  let authRequired = 0;
  const api = createReadApi({ onAuthRequired: () => (authRequired += 1) });

  await assert.rejects(api.getUsers(), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_REQUIRED');
  assert.equal(authRequired, 1);

  respond = errorResponse(403, 'AUTH_FORBIDDEN');
  await assert.rejects(api.getUsers(), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_FORBIDDEN');
  assert.equal(authRequired, 1);
});

test('서버 계정 조회는 세션 헤더를 전달하고 캐시하지 않는다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = realFetch; });
  const user = {
    account_type: 'TENANT', tenant_id: 'tenant-1', role: 'VIEWER',
    user_id: 'user-1', email: 'viewer@example.invalid',
    session: { absolute_expires_at: '2026-10-27T00:00:00Z', idle_expires_at: '2026-10-26T17:00:00Z' },
  };
  let calls = 0;
  globalThis.fetch = async (url, init) => {
    calls += 1;
    assert.equal(new URL(String(url)).pathname, '/api/v1/auth/me');
    assert.equal(new Headers(init?.headers).get('Cookie'), '__Host-vg_session=test-session');
    assert.equal(init?.cache, 'no-store');
    return new Response(JSON.stringify(user), { status: 200 });
  };
  const api = createReadApi({ headers: { Cookie: '__Host-vg_session=test-session' } });
  assert.deepEqual(await api.getMe(), user);
  assert.equal(calls, 1);
});

test('계정 조회 실패 — SSR 401은 redirect를 전파하고 503은 계정 없음으로 바꾸지 않는다', async (t) => {
  let respond = errorResponse(401, 'AUTH_REQUIRED');
  stubFetch(t, () => respond());
  const redirect = new Error('server redirect');
  let redirects = 0;
  const api = createReadApi({ onAuthRequired: () => { redirects += 1; throw redirect; } });
  await assert.rejects(api.getMe(), (error) => error === redirect);
  assert.equal(redirects, 1);
  respond = errorResponse(503, 'INTERNAL_ERROR');
  await assert.rejects(api.getMe(), (error: unknown) => error instanceof ApiError && error.httpStatus === 503);
  assert.equal(redirects, 1);
});

test('브라우저 계정 조회는 기존 401 null 계약을 유지한다', async (t) => {
  stubFetch(t, errorResponse(401, 'AUTH_REQUIRED'));
  assert.equal(await getMe(), null);
});

/** 방금 비운 포트 — 연결하면 실제 Node 연결 거부 오류(`cause.code = ECONNREFUSED`)가 난다. */
async function closedPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address !== null && typeof address === 'object');
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return address.port;
}

test('연결 거부는 원인 코드를 문구에 남기고, 앱 코드에서 새로 만든 오류로 올린다', async (t) => {
  const saved = process.env.NEXT_PUBLIC_API_BASE_URL;
  t.after(() => {
    if (saved === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
    else process.env.NEXT_PUBLIC_API_BASE_URL = saved;
  });
  process.env.NEXT_PUBLIC_API_BASE_URL = `http://127.0.0.1:${await closedPort()}`;

  await assert.rejects(getAssets(), (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, '요청이 실패했습니다 (네트워크 오류: ECONNREFUSED)');
    // cause가 붙으면 스택이 `node:` 내부 프레임뿐인 원래 오류가 서버 컴포넌트 prop 직렬화로 그대로 넘어간다.
    assert.equal('cause' in error, false);
    // 앱 코드 프레임이 남아야 dev 직렬화가 깨지지 않는다 — fetch 오류를 되던지지 않고 새로 만들었다는 증거.
    assert.match(String(error.stack), /client\.ts/);
    return true;
  });
});

test('원인 코드가 없는 실패(브라우저의 Failed to fetch)는 코드 없이 접는다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = realFetch;
  });
  globalThis.fetch = () => Promise.reject(new TypeError('Failed to fetch'));

  await assert.rejects(getAssets(), {
    message: '요청이 실패했습니다 (네트워크 오류)',
  });
});
