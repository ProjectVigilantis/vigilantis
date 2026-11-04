// 고객 설정(T-7) 변경분 계산 · 입력 검사 · 새로고침 병합 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  EMAIL_LEGACY_LABEL,
  EMAIL_STATUS_LABELS,
  draftFrom,
  emailRetryRequest,
  emailStatusEntry,
  hasChanges,
  isEmailAddress,
  isSlackWebhookUrl,
  rebaseDraft,
  settingsDenial,
  settingsScope,
  settingsUpdate,
  settingsViewAfter,
  type SettingsDraft,
} from './tenant-settings.ts';
import type { EmailNotificationStatus, ErrorCode, NotificationSettings, TenantSettings } from '../types/api.ts';

const WEBHOOK = 'https://hooks.slack.com/services/T000/B000/abc123';

function settings(overrides: Partial<Omit<TenantSettings, 'notifications'>> & { notifications?: Partial<NotificationSettings> } = {}): TenantSettings {
  const { notifications, ...rest } = overrides;
  return {
    tenant_id: '123e4567-e89b-12d3-a456-426614174000',
    protected_tags: [{ key: 'Env', values: ['prod'] }],
    notifications: { slack_configured: false, email_address: null, email_status: 'UNSET', legacy_sns_configured: false, ...notifications },
    updated_at: '2026-10-30T00:00:00Z',
    ...rest,
  };
}

const edit = (saved: TenantSettings, patch: Partial<SettingsDraft>): SettingsDraft => ({ ...draftFrom(saved), ...patch });

test('처음 읽은 상태는 바뀐 것이 없다 — 보내지 않고, Webhook 입력은 비어 있다', () => {
  const saved = settings({ notifications: { slack_configured: true, email_address: 'a@b.c', email_status: 'READY' } });
  const draft = draftFrom(saved);
  assert.equal(draft.slackUrl, '');
  assert.equal(draft.email, 'a@b.c');
  assert.deepEqual(settingsUpdate(saved, draft), { ok: true, request: null });
  assert.equal(hasChanges(saved, draft), false);
});

test('바뀐 항목만 싣는다 — 태그만 바꾸면 notifications를 보내지 않는다', () => {
  const saved = settings();
  const result = settingsUpdate(saved, edit(saved, { tags: [{ key: 'Env', values: ['prod', 'stage'] }] }));
  assert.deepEqual(result, { ok: true, request: { protected_tags: [{ key: 'Env', values: ['prod', 'stage'] }] } });
});

test('보호 태그 전체 비우기는 [] 교체로 보낸다', () => {
  const saved = settings();
  assert.deepEqual(settingsUpdate(saved, edit(saved, { tags: [] })), { ok: true, request: { protected_tags: [] } });
});

test('태그 키 · 값은 다듬지 않고 그대로 보낸다 — 빈 값과 공백도 값이다', () => {
  const saved = settings({ protected_tags: [] });
  const tags = [{ key: ' Owner ', values: ['', ' team a '] }];
  assert.deepEqual(settingsUpdate(saved, edit(saved, { tags })), { ok: true, request: { protected_tags: tags } });
});

test('태그 검사 — 빈 키 · 값 없음 · 대소문자만 다른 키 · 공백 · 대소문자만 다른 값은 막는다', () => {
  const saved = settings({ protected_tags: [] });
  const rejected = [
    [{ key: '', values: ['x'] }],
    [{ key: 'Env', values: [] }],
    [{ key: 'Env', values: ['a'] }, { key: 'ENV', values: ['b'] }],
    [{ key: 'Env', values: ['Prod', ' prod '] }],
  ];
  for (const tags of rejected) {
    const result = settingsUpdate(saved, edit(saved, { tags }));
    assert.equal(result.ok, false, JSON.stringify(tags));
    assert.ok(!result.ok && typeof result.errors.tags === 'string');
  }
});

test('Slack — 새 URL은 설정 · 교체, 해제는 연결이 있을 때만 null', () => {
  const unset = settings();
  assert.deepEqual(settingsUpdate(unset, edit(unset, { slackUrl: WEBHOOK })), {
    ok: true,
    request: { notifications: { slack_webhook_url: WEBHOOK } },
  });
  const configured = settings({ notifications: { slack_configured: true } });
  assert.deepEqual(settingsUpdate(configured, edit(configured, { slackUrl: WEBHOOK })), {
    ok: true,
    request: { notifications: { slack_webhook_url: WEBHOOK } },
  });
  assert.deepEqual(settingsUpdate(configured, edit(configured, { slackRemove: true })), {
    ok: true,
    request: { notifications: { slack_webhook_url: null } },
  });
  // 연결이 없는데 해제를 누른 상태는 보낼 것이 없다(빈 notifications를 만들지 않는다).
  assert.deepEqual(settingsUpdate(unset, edit(unset, { slackRemove: true })), { ok: true, request: null });
});

test('Slack URL은 계약 패턴과 전체 일치해야 한다', () => {
  assert.equal(isSlackWebhookUrl(WEBHOOK), true);
  for (const bad of [
    'http://hooks.slack.com/services/T/B/x',
    'https://hooks.slack.com/services/T/B',
    'https://hooks.slack.com/services/T/B/x-y',
    'https://hooks.slack.com/services/T/B/x/',
    `${WEBHOOK} `,
    // Python fullmatch는 끝 줄바꿈을 거절한다 — 화면 검사도 같아야 한다.
    `${WEBHOOK}\n`,
    `\n${WEBHOOK}`,
    'https://evil.example/services/T/B/x',
  ]) {
    assert.equal(isSlackWebhookUrl(bad), false, bad);
  }
  const saved = settings();
  const result = settingsUpdate(saved, edit(saved, { slackUrl: 'https://hooks.slack.com/services/T/B' }));
  assert.ok(!result.ok && typeof result.errors.slack === 'string');
  // 오류 문구에 입력한 비밀값을 싣지 않는다.
  assert.ok(!result.ok && !result.errors.slack?.includes('services/T/B'));
});

test('이메일 — 설정 · 변경은 주소, 비우면 null, 형식이 틀리면 보내지 않는다', () => {
  const unset = settings();
  assert.deepEqual(settingsUpdate(unset, edit(unset, { email: 'ops@example.com' })), {
    ok: true,
    request: { notifications: { email_address: 'ops@example.com' } },
  });
  const set = settings({ notifications: { email_address: 'ops@example.com', email_status: 'CONFIRMATION_PENDING' } });
  assert.deepEqual(settingsUpdate(set, edit(set, { email: 'sec@example.com' })), {
    ok: true,
    request: { notifications: { email_address: 'sec@example.com' } },
  });
  assert.deepEqual(settingsUpdate(set, edit(set, { email: '' })), {
    ok: true,
    request: { notifications: { email_address: null } },
  });
  for (const bad of ['ops', 'a b@c.d', ' ops@example.com', 'a@b@c', 'ops@example.com\n']) {
    assert.equal(isEmailAddress(bad), false, bad);
    const result = settingsUpdate(unset, edit(unset, { email: bad }));
    assert.ok(!result.ok && typeof result.errors.email === 'string', bad);
  }
  // 계약 상한 254자: 254자는 받고 255자는 막는다.
  assert.equal(isEmailAddress(`${'a'.repeat(250)}@b.c`), true);
  assert.equal(isEmailAddress(`${'a'.repeat(251)}@b.c`), false);
});

test('Slack과 이메일을 함께 바꾸면 notifications 하나에 둘 다 싣는다', () => {
  const saved = settings({ notifications: { slack_configured: true, email_address: 'ops@example.com', email_status: 'READY' } });
  assert.deepEqual(settingsUpdate(saved, edit(saved, { slackRemove: true, email: '' })), {
    ok: true,
    request: { notifications: { slack_webhook_url: null, email_address: null } },
  });
});

test('저장 성공 뒤 응답으로 다시 만든 초안은 Webhook 입력을 비우고 바뀐 것이 없다', () => {
  const response = settings({ notifications: { slack_configured: true } });
  const draft = draftFrom(response);
  assert.equal(draft.slackUrl, '');
  assert.equal(draft.slackRemove, false);
  assert.equal(hasChanges(response, draft), false);
});

test('상태 새로고침 — 손대지 않은 칸만 새 값을 따르고, 고치던 칸과 새 Webhook 입력은 남긴다', () => {
  const before = settings({ notifications: { email_address: 'ops@example.com', email_status: 'SETUP_PENDING' } });
  const after = settings({
    protected_tags: [{ key: 'Env', values: ['prod', 'live'] }],
    notifications: { email_address: 'ops@example.com', email_status: 'CONFIRMATION_PENDING' },
  });
  // 손대지 않은 초안은 새 값을 그대로 따른다.
  assert.deepEqual(rebaseDraft(draftFrom(before), before, after), draftFrom(after));

  const dirty = edit(before, { tags: [{ key: 'Owner', values: ['me'] }], slackUrl: WEBHOOK, email: 'sec@example.com' });
  assert.deepEqual(rebaseDraft(dirty, before, after), dirty);
});

test('상태 새로고침 — 그 사이 Slack 연결이 사라졌으면 해제 표시를 거둔다', () => {
  const before = settings({ notifications: { slack_configured: true } });
  const after = settings();
  assert.equal(rebaseDraft(edit(before, { slackRemove: true }), before, after).slackRemove, false);
});

test('이메일 재연결 — 연결 실패한 저장 주소만 같은 주소로 다시 보낸다', () => {
  const failed = settings({ notifications: { email_address: 'ops@example.com', email_status: 'SETUP_FAILED' } });
  assert.deepEqual(emailRetryRequest(failed), { notifications: { email_address: 'ops@example.com' } });
  for (const status of ['SETUP_PENDING', 'CONFIRMATION_PENDING', 'READY'] as const) {
    assert.equal(emailRetryRequest(settings({ notifications: { email_address: 'ops@example.com', email_status: status } })), null, status);
  }
  assert.equal(emailRetryRequest(settings()), null);
  // 재연결 버튼 밖의 보통 저장은 같은 주소를 바뀐 것으로 보지 않는다.
  assert.deepEqual(settingsUpdate(failed, draftFrom(failed)), { ok: true, request: null });
});

test('이메일 재연결 응답은 다른 칸의 저장하지 않은 입력을 남기고 이메일 칸만 새 상태를 따른다', () => {
  const before = settings({ notifications: { email_address: 'ops@example.com', email_status: 'SETUP_FAILED' } });
  const after = settings({ notifications: { email_address: 'ops@example.com', email_status: 'SETUP_PENDING' } });
  const dirty = edit(before, { tags: [{ key: 'Owner', values: ['me'] }], slackUrl: WEBHOOK });
  const next = rebaseDraft(dirty, before, after);
  assert.deepEqual(next, dirty);
  // 남은 입력은 여전히 저장할 변경이고, 이메일은 바뀐 것이 아니다.
  assert.deepEqual(settingsUpdate(after, next), {
    ok: true,
    request: { protected_tags: [{ key: 'Owner', values: ['me'] }], notifications: { slack_webhook_url: WEBHOOK } },
  });
});

const TENANT = '123e4567-e89b-12d3-a456-426614174000';
const OTHER = '223e4567-e89b-12d3-a456-426614174000';
const customer = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER', user_id = 'u1') => ({
  account_type: 'TENANT' as const, tenant_id: TENANT, platform_role: null, role, assigned_tenant_ids: [], user_id,
});
const manager = (...assigned: string[]) => ({
  account_type: 'PLATFORM' as const, tenant_id: null, platform_role: 'TENANT_MANAGER' as const, role: 'ADMIN' as const,
  assigned_tenant_ids: assigned, user_id: 'p1',
});

test('폼 범위 — 같은 계정 · 권한이면 같은 키, 계정 교체 · 역할 하향은 다른 키, 읽기 권한이 없으면 null', () => {
  const admin = settingsScope(customer('ADMIN'), TENANT);
  assert.ok(admin !== null && admin.write);
  assert.deepEqual(settingsScope(customer('ADMIN'), TENANT.toUpperCase()), admin);

  const viewer = settingsScope(customer('VIEWER'), TENANT);
  assert.ok(viewer !== null && !viewer.write);
  assert.notEqual(viewer.key, admin.key);
  assert.notEqual(settingsScope(customer('ADMIN', 'u2'), TENANT)?.key, admin.key);

  assert.equal(settingsScope(customer('ADMIN'), OTHER), null);
  assert.equal(settingsScope(null, TENANT), null);
});

test('폼 범위 — 담당 배정이 회수되면 null, 이 고객과 무관한 배정 변경은 키를 바꾸지 않는다', () => {
  const assigned = settingsScope(manager(TENANT), TENANT);
  assert.ok(assigned !== null && assigned.write);
  assert.deepEqual(settingsScope(manager(TENANT, OTHER), TENANT), assigned);
  assert.equal(settingsScope(manager(OTHER), TENANT), null);
  assert.equal(settingsScope(manager(), TENANT), null);
});

test('거절 판정은 요청 종류를 본다 — 조회 403 · 범위 밖 404는 조회 거절, 저장 403은 쓰기 거절', () => {
  assert.equal(settingsDenial('read', 'AUTH_FORBIDDEN'), 'read');
  assert.equal(settingsDenial('read', 'TENANT_NOT_FOUND'), 'read');
  assert.equal(settingsDenial('write', 'AUTH_FORBIDDEN'), 'write');
  // 저장 중에 범위 밖이 되면(배정 회수) 볼 수도 없다.
  assert.equal(settingsDenial('write', 'TENANT_NOT_FOUND'), 'read');
  for (const code of ['REQUEST_VALIDATION_FAILED', 'TENANT_SUSPENDED', 'INTERNAL_ERROR', null] as const) {
    assert.equal(settingsDenial('read', code), null, String(code));
    assert.equal(settingsDenial('write', code), null, String(code));
  }
});

const failed = (operation: 'read' | 'write', code: ErrorCode | null) => ({ type: 'failed' as const, operation, code, error: code });

test('조회 거절이면 저장값을 버리고 거절 화면으로 — 새로 성공한 조회로만 돌아온다', () => {
  const initial = settings({ notifications: { email_address: 'ops@example.com', email_status: 'READY' } });
  const ready = { kind: 'ready' as const, saved: initial };

  for (const event of [failed('read', 'TENANT_NOT_FOUND'), failed('read', 'AUTH_FORBIDDEN'), failed('write', 'TENANT_NOT_FOUND')]) {
    const denied = settingsViewAfter(ready, event);
    assert.deepEqual(denied, { kind: 'denied', error: event.code }, JSON.stringify(event));
    // 거절 화면은 수신 주소 · 태그를 들고 있지 않다.
    assert.equal('saved' in denied, false);
  }

  const denied = settingsViewAfter(ready, failed('read', 'TENANT_NOT_FOUND'));
  // 다시 확인이 또 실패하면 거절 그대로다(처음 받은 값으로 되돌리지 않는다).
  assert.deepEqual(settingsViewAfter(denied, failed('read', 'INTERNAL_ERROR')), denied);
  assert.deepEqual(settingsViewAfter(denied, failed('read', 'AUTH_FORBIDDEN')), { kind: 'denied', error: 'AUTH_FORBIDDEN' });
  const fresh = settings({ protected_tags: [], updated_at: '2026-10-30T01:00:00Z' });
  assert.deepEqual(settingsViewAfter(denied, { type: 'loaded', settings: fresh }), { kind: 'ready', saved: fresh });
});

test('저장 403 · 입력 오류 · 일시 오류는 화면을 내리지 않는다 — 저장값이 그대로다', () => {
  const ready = { kind: 'ready' as const, saved: settings() };
  for (const event of [failed('write', 'AUTH_FORBIDDEN'), failed('write', 'REQUEST_VALIDATION_FAILED'), failed('read', 'INTERNAL_ERROR'), failed('read', null)]) {
    assert.equal(settingsViewAfter(ready, event), ready, JSON.stringify(event));
  }
  const next = settings({ protected_tags: [] });
  assert.deepEqual(settingsViewAfter(ready, { type: 'loaded', settings: next }), { kind: 'ready', saved: next });
});

test('ASCII 밖 문자가 든 저장 주소도 계약상 유효하다 — 태그만 바꾼 저장이 막히지 않고 주소를 다시 보내지 않는다', () => {
  assert.equal(isEmailAddress('사용자@example.com'), true);
  const saved = settings({ notifications: { email_address: '사용자@example.com', email_status: 'READY' } });
  assert.deepEqual(settingsUpdate(saved, edit(saved, { tags: [] })), { ok: true, request: { protected_tags: [] } });
  const unset = settings();
  assert.deepEqual(settingsUpdate(unset, edit(unset, { email: '보안팀@예시.한국' })), {
    ok: true,
    request: { notifications: { email_address: '보안팀@예시.한국' } },
  });
});

const LEGACY = { legacy_sns_configured: true } as const;

test('이전 방식 SNS 경로는 미설정과 다른 표시다 — 관리 주소가 있으면 그 상태를 따른다', () => {
  assert.equal(emailStatusEntry(settings({ notifications: LEGACY }).notifications), EMAIL_LEGACY_LABEL);
  assert.notEqual(EMAIL_LEGACY_LABEL.label, EMAIL_STATUS_LABELS.UNSET.label);
  assert.equal(emailStatusEntry(settings().notifications), EMAIL_STATUS_LABELS.UNSET);
  const managed = settings({ notifications: { email_address: 'ops@example.com', email_status: 'READY' } });
  assert.equal(emailStatusEntry(managed.notifications), EMAIL_STATUS_LABELS.READY);
});

test('이전 방식 경로는 열거나 새로고침만으로 해제하지 않는다 — 명시한 해제만 email_address:null을 보낸다', () => {
  const saved = settings({ notifications: LEGACY });
  assert.equal(draftFrom(saved).legacyEmailRemove, false);
  assert.deepEqual(settingsUpdate(saved, draftFrom(saved)), { ok: true, request: null });
  const refreshed = rebaseDraft(draftFrom(saved), saved, saved);
  assert.deepEqual(settingsUpdate(saved, refreshed), { ok: true, request: null });

  // 주소 칸은 이미 비어 있지만 해제는 보낸다. 다른 칸 변경과 함께 실린다.
  assert.deepEqual(settingsUpdate(saved, edit(saved, { legacyEmailRemove: true })), {
    ok: true,
    request: { notifications: { email_address: null } },
  });
  assert.deepEqual(settingsUpdate(saved, edit(saved, { legacyEmailRemove: true, tags: [] })), {
    ok: true,
    request: { protected_tags: [], notifications: { email_address: null } },
  });
  // 해제 취소(false)는 다시 바뀐 것이 없다.
  assert.equal(hasChanges(saved, edit(saved, { legacyEmailRemove: false })), false);
  // 이전 방식 경로가 없으면 해제 표시가 남아도 보내지 않는다.
  const plain = settings();
  assert.deepEqual(settingsUpdate(plain, edit(plain, { legacyEmailRemove: true })), { ok: true, request: null });
});

test('새 주소를 넣으면 이전 방식 경로를 대신한다 — 그 주소만 보낸다', () => {
  const saved = settings({ notifications: LEGACY });
  assert.deepEqual(settingsUpdate(saved, edit(saved, { email: 'ops@example.com' })), {
    ok: true,
    request: { notifications: { email_address: 'ops@example.com' } },
  });
  assert.deepEqual(settingsUpdate(saved, edit(saved, { email: 'ops@example.com', legacyEmailRemove: true })), {
    ok: true,
    request: { notifications: { email_address: 'ops@example.com' } },
  });
});

test('새로고침은 이전 방식 해제 표시를 남기되, 그 경로가 사라졌으면 거둔다 — 다른 입력은 그대로다', () => {
  const before = settings({ notifications: LEGACY });
  const draft = edit(before, { legacyEmailRemove: true, slackUrl: WEBHOOK });
  const still = rebaseDraft(draft, before, settings({ notifications: LEGACY, updated_at: '2026-10-30T02:00:00Z' }));
  assert.equal(still.legacyEmailRemove, true);
  assert.equal(still.slackUrl, WEBHOOK);
  const gone = rebaseDraft(draft, before, settings());
  assert.equal(gone.legacyEmailRemove, false);
  assert.equal(gone.slackUrl, WEBHOOK);
});

test('저장 응답으로 다시 그리면 해제 표시를 비운다 — 경로가 없어진 응답에는 다시 보낼 것이 없다', () => {
  const response = settings();
  const draft = draftFrom(response);
  assert.equal(draft.legacyEmailRemove, false);
  assert.equal(hasChanges(response, draft), false);
});

test('이메일 상태 5종은 서로 다른 표시로 구분된다', () => {
  const statuses: EmailNotificationStatus[] = ['UNSET', 'SETUP_PENDING', 'SETUP_FAILED', 'CONFIRMATION_PENDING', 'READY'];
  assert.equal(new Set(statuses.map((s) => EMAIL_STATUS_LABELS[s].label)).size, statuses.length);
});
