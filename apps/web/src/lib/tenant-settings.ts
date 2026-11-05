// T-7 고객 설정(보호 태그 · 알림 채널) 화면의 입력 검사 · 변경분 계산 · 상태 문구.
// 원천은 packages/schemas/api/settings.py다. 여기 검사는 서버가 422로 거절할 요청을 칸 옆에 미리 알리는 편의이고,
// 최종 판정(입력 · 권한 · 범위)은 서버가 요청마다 다시 한다.
//
// PATCH 규칙: 생략은 유지, 보호 태그는 배열 전체 교체([]는 전부 해제), 채널의 null은 해제다. 바뀐 것이 없으면
// 보내지 않는다(빈 PATCH · 빈 notifications는 서버가 거부한다). 태그 키 · 값은 입력 그대로 보낸다 — 다듬지 않는다.
// 예외 둘: 이메일 연결 실패(SETUP_FAILED)의 재시도는 같은 주소를 명시적으로 다시 보낸다(`emailRetryRequest`).
// 관리 주소 없이 남은 이전 방식 SNS 경로는 주소 칸이 이미 비어 있어도 명시한 해제(`legacyEmailRemove`)만 null을 보낸다.
// Slack Webhook URL은 쓰기 전용 비밀값이다. 응답에 오지 않으므로 초안에는 새로 입력한 값만 있고, 저장되면 비운다.

import type { EnumLabel } from './enum-labels.ts';
import { tenantSettingsAccess, type UserAdminSubject } from './roles.ts';
import type {
  CurrentUserResponse,
  EmailNotificationStatus,
  ErrorCode,
  NotificationSettings,
  NotificationSettingsUpdateRequest,
  ProtectedTagRule,
  TenantSettings,
  TenantSettingsUpdateRequest,
} from '../types/api.ts';

/** 설정 요청의 종류 — 조회(GET)와 저장 · 재연결(PATCH). */
export type SettingsOperation = 'read' | 'write';

/**
 * 서버 거절이 무엇을 막았나. `read` = 이 고객 설정을 더 볼 수 없다(GET 403, 또는 어떤 요청이든 범위 밖 404
 * `TENANT_NOT_FOUND` — 배정 회수 포함). `write` = 바꾸기만 막혔다(PATCH 403 — 조회는 여전히 될 수 있다).
 * 그 밖의 실패(422 · 409 · 5xx · 네트워크)는 null — 입력을 남기고 오류만 보인다. 거절이면 화면은 `/me`를 다시 읽는다.
 */
export function settingsDenial(operation: SettingsOperation, code: ErrorCode | null): SettingsOperation | null {
  if (code === 'TENANT_NOT_FOUND') return 'read';
  if (code === 'AUTH_FORBIDDEN') return operation;
  return null;
}

/**
 * 화면에 무엇을 보이나. `ready` = 마지막으로 받은 저장값으로 폼을 그린다. `denied` = 조회 거절 — 저장값 · 입력을
 * 버리고 안내만 그린다. `denied`에서 돌아오는 길은 새로 성공한 조회(`loaded`)뿐이다(처음 받은 값으로 되돌리지 않는다).
 */
export type SettingsView = { kind: 'ready'; saved: TenantSettings } | { kind: 'denied'; error: unknown };

export type SettingsEvent =
  /** 조회 · 저장 응답(200 TenantSettings). */
  | { type: 'loaded'; settings: TenantSettings }
  | { type: 'failed'; operation: SettingsOperation; code: ErrorCode | null; error: unknown };

export function settingsViewAfter(view: SettingsView, event: SettingsEvent): SettingsView {
  if (event.type === 'loaded') return { kind: 'ready', saved: event.settings };
  if (settingsDenial(event.operation, event.code) === 'read') return { kind: 'denied', error: event.error };
  return view;
}

/** 폼을 그릴 계정 · 권한 단위. 키가 바뀌면 화면이 폼을 새로 만들어 저장하지 않은 입력(Webhook URL 포함)을 버린다. */
export interface SettingsScope {
  key: string;
  write: boolean;
}

/**
 * 이 고객 설정을 지금 계정으로 보여 줄 범위. 읽기 권한이 없거나 계정을 모르면 null — 폼을 그리지 않는다.
 * 키는 사용자 · 고객 · 쓰기 여부다. 다른 계정으로 바뀌거나, 역할이 내려가거나, 배정이 회수되면 키가 바뀌거나 null이
 * 된다. 이 고객과 무관한 배정 변경은 키를 바꾸지 않는다.
 */
export function settingsScope(
  user: (UserAdminSubject & Pick<CurrentUserResponse, 'user_id'>) | null,
  tenantId: string,
): SettingsScope | null {
  const access = tenantSettingsAccess(user, tenantId);
  if (user === null || !access.read) return null;
  return { key: `${user.user_id} ${tenantId.toLowerCase()} ${access.write ? 'write' : 'read'}`, write: access.write };
}

/** packages/schemas/slack.py `_WEBHOOK_RE`와 같은 패턴(전체 일치). */
const SLACK_WEBHOOK = /^https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9]+\/[A-Za-z0-9]+\/[A-Za-z0-9]+$/;
/** packages/schemas/api/auth.py `Email`과 같은 모양(3–254자). */
const EMAIL = /^[^@\s]+@[^@\s]+$/;
const EMAIL_MIN = 3;
const EMAIL_MAX = 254;

export function isSlackWebhookUrl(value: string): boolean {
  return SLACK_WEBHOOK.test(value);
}

export function isEmailAddress(value: string): boolean {
  return value.length >= EMAIL_MIN && value.length <= EMAIL_MAX && EMAIL.test(value);
}

/** 화면이 들고 있는 입력. 저장값과 비교해 바뀐 것만 보낸다(`settingsUpdate`). */
export interface SettingsDraft {
  tags: ProtectedTagRule[];
  /** 새로 입력한 Webhook URL. 저장된 URL은 받지 않으므로 처음에는 언제나 빈 값이다. */
  slackUrl: string;
  /** 저장된 Slack 연결을 해제한다. 연결이 없으면 보내지 않는다. */
  slackRemove: boolean;
  /** 빈 값이면 주소 없음(해제)이다. */
  email: string;
  /** 이전 방식 SNS 이메일 경로를 해제한다. 그 경로가 없거나 새 주소를 넣으면 보내지 않는다(새 주소가 대신한다). */
  legacyEmailRemove: boolean;
}

const copyTags = (tags: readonly ProtectedTagRule[]): ProtectedTagRule[] =>
  tags.map((rule) => ({ key: rule.key, values: [...rule.values] }));

export function draftFrom(settings: TenantSettings): SettingsDraft {
  return {
    tags: copyTags(settings.protected_tags),
    slackUrl: '',
    slackRemove: false,
    email: settings.notifications.email_address ?? '',
    legacyEmailRemove: false,
  };
}

function sameTags(a: readonly ProtectedTagRule[], b: readonly ProtectedTagRule[]): boolean {
  return (
    a.length === b.length &&
    a.every(
      (rule, i) =>
        rule.key === b[i].key &&
        rule.values.length === b[i].values.length &&
        rule.values.every((value, j) => value === b[i].values[j]),
    )
  );
}

export interface SettingsErrors {
  tags?: string;
  slack?: string;
  email?: string;
}

export type SettingsUpdateResult =
  | { ok: true; request: TenantSettingsUpdateRequest | null }
  | { ok: false; errors: SettingsErrors };

/** 서버 비교 기준: 키는 대소문자 무시, 값은 양끝 공백 제거 · 대소문자 무시(settings.py). */
const keyOf = (key: string) => key.toLowerCase();
const valueOf = (value: string) => value.trim().toLowerCase();

function tagsError(tags: readonly ProtectedTagRule[]): string | null {
  const keys = new Set<string>();
  for (const [i, rule] of tags.entries()) {
    const at = `${i + 1}번째 규칙`;
    if (rule.key === '') return `${at}: 태그 키를 넣으세요.`;
    if (keys.has(keyOf(rule.key))) return `${at}: 같은 태그 키가 이미 있습니다(대소문자 무시): ${rule.key}`;
    keys.add(keyOf(rule.key));
    if (rule.values.length === 0) return `${at}: 값을 하나 이상 넣으세요. 빈 값도 하나의 값입니다.`;
    const values = new Set<string>();
    for (const value of rule.values) {
      if (values.has(valueOf(value))) return `${at}: 같은 값이 두 번 있습니다(앞뒤 공백 · 대소문자 무시): "${value}"`;
      values.add(valueOf(value));
    }
  }
  return null;
}

/**
 * 저장값과 입력을 비교해 PATCH 본문을 만든다. 바뀐 것이 없으면 `request: null` — 보내지 않는다.
 * 보호 태그는 바뀌었을 때만 목록 전체를, 알림은 바뀐 채널만 싣는다.
 */
export function settingsUpdate(saved: TenantSettings, draft: SettingsDraft): SettingsUpdateResult {
  const errors: SettingsErrors = {};
  const request: TenantSettingsUpdateRequest = {};
  const notifications: NotificationSettingsUpdateRequest = {};

  if (!sameTags(draft.tags, saved.protected_tags)) {
    const error = tagsError(draft.tags);
    if (error !== null) errors.tags = error;
    else request.protected_tags = copyTags(draft.tags);
  }

  if (draft.slackRemove) {
    if (saved.notifications.slack_configured) notifications.slack_webhook_url = null;
  } else if (draft.slackUrl !== '') {
    if (isSlackWebhookUrl(draft.slackUrl)) notifications.slack_webhook_url = draft.slackUrl;
    else errors.slack = 'Slack Incoming Webhook URL이 아닙니다(https://hooks.slack.com/services/…/…/…).';
  }

  if (draft.email !== (saved.notifications.email_address ?? '')) {
    if (draft.email === '') notifications.email_address = null;
    else if (isEmailAddress(draft.email)) notifications.email_address = draft.email;
    else errors.email = '이메일 주소 형식이 아닙니다.';
  } else if (draft.legacyEmailRemove && draft.email === '' && saved.notifications.legacy_sns_configured) {
    // 관리 주소가 없어도(null) 이전 방식 경로는 남아 있다 — 명시한 해제만 null을 보낸다.
    notifications.email_address = null;
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  if (Object.keys(notifications).length > 0) request.notifications = notifications;
  return { ok: true, request: Object.keys(request).length > 0 ? request : null };
}

/**
 * 연결 실패(SETUP_FAILED)한 저장 주소를 그대로 다시 연결하는 요청. 계약상 이 상태에서 같은 주소를 보내면 재시도다.
 * 그 밖의 상태에서는 null — 보통 저장에서 같은 주소는 바뀐 것이 아니라 보내지 않는다(`settingsUpdate`).
 */
export function emailRetryRequest(saved: TenantSettings): TenantSettingsUpdateRequest | null {
  const { email_address: address, email_status: status } = saved.notifications;
  if (status !== 'SETUP_FAILED' || address === null) return null;
  return { notifications: { email_address: address } };
}

/** 저장하지 않은 입력이 있나 — 형식이 틀린 입력도 바뀐 것이다. */
export function hasChanges(saved: TenantSettings, draft: SettingsDraft): boolean {
  const result = settingsUpdate(saved, draft);
  return !result.ok || result.request !== null;
}

/**
 * 다시 읽은 설정 위에 저장하지 않은 입력을 얹는다(상태 새로고침). 이전 저장값 그대로인 칸만 새 값을 따르고,
 * 고치던 칸과 새로 입력한 Webhook URL은 그대로 둔다. 연결이 그 사이 사라졌으면 해제 표시는 거둔다.
 */
export function rebaseDraft(draft: SettingsDraft, before: TenantSettings, after: TenantSettings): SettingsDraft {
  const previous = draftFrom(before);
  const next = draftFrom(after);
  return {
    tags: sameTags(draft.tags, previous.tags) ? next.tags : draft.tags,
    slackUrl: draft.slackUrl,
    slackRemove: draft.slackRemove && after.notifications.slack_configured,
    email: draft.email === previous.email ? next.email : draft.email,
    legacyEmailRemove: draft.legacyEmailRemove && after.notifications.legacy_sns_configured,
  };
}

export const SLACK_STATUS_LABELS: Record<'configured' | 'unset', EnumLabel> = {
  configured: { label: '연결됨', tone: 'green' },
  unset: { label: '미설정', tone: 'gray' },
};

/**
 * 이메일 채널 상태. READY는 받는 사람이 구독을 확인해 경로가 준비된 상태이고, 개별 메일의 도착을 뜻하지 않는다.
 * 구독 확인은 받는 사람이 직접 한다 — 화면은 확인을 대신하지 않는다.
 */
export const EMAIL_STATUS_LABELS: Record<EmailNotificationStatus, EnumLabel & { hint: string }> = {
  UNSET: { label: '미설정', tone: 'gray', hint: '이메일 알림을 받을 주소가 없습니다.' },
  SETUP_PENDING: {
    label: '연결 준비 중',
    tone: 'gray',
    hint: '수신 구독을 만드는 중입니다. 잠시 뒤 [상태 새로고침]으로 다시 확인하세요.',
  },
  SETUP_FAILED: {
    label: '연결 실패',
    tone: 'red',
    hint: '수신 구독 연결에 실패했습니다. 같은 주소로 다시 연결하거나, 주소를 바꿔 저장할 수 있습니다.',
  },
  CONFIRMATION_PENDING: {
    label: '수신 확인 대기',
    tone: 'yellow',
    hint: '받는 사람이 구독 확인 메일의 링크를 직접 눌러야 알림이 나갑니다. 메일함(스팸함 포함)을 확인하게 한 뒤 [상태 새로고침]을 누르세요.',
  },
  READY: { label: '수신 확인됨', tone: 'green', hint: '구독 확인 · 발송 준비 완료.' },
};

/** 관리 주소 없이 이전 방식 SNS 경로만 있을 때. 받는 주소 · 구독 확인 여부는 알 수 없다. */
export const EMAIL_LEGACY_LABEL: EnumLabel & { hint: string } = {
  label: '기존 SNS 경로',
  tone: 'yellow',
  hint:
    '이전 방식으로 설정한 SNS 이메일 경로로 알림을 보내고 있습니다. 이 화면에서는 받는 주소나 구독 확인 여부를 알 수 없습니다. ' +
    '새 주소를 저장하면 그 주소로 바뀌고, [기존 경로 해제]를 저장하면 이 경로로 보내지 않습니다.',
};

/** 이메일 채널 표시. 관리 주소가 없고(UNSET) 이전 방식 경로가 있으면 미설정 대신 그 경로를 보인다. */
export function emailStatusEntry(notifications: NotificationSettings): EnumLabel & { hint: string } {
  return notifications.email_status === 'UNSET' && notifications.legacy_sns_configured
    ? EMAIL_LEGACY_LABEL
    : EMAIL_STATUS_LABELS[notifications.email_status];
}

export const SETTINGS_SAVED = '설정을 저장했습니다. 보호 태그는 다음 평가부터 적용됩니다.';
export const EMAIL_RETRY_STARTED = '같은 주소로 수신 구독 연결을 다시 시작했습니다. 잠시 뒤 [상태 새로고침]으로 확인하세요.';
export const SETTINGS_ACCOUNT_PENDING = '계정을 확인한 뒤 설정을 보여 줍니다.';
export const SETTINGS_READ_DENIED =
  '서버가 이 고객 설정의 조회를 거절해 화면에서 내렸습니다. 역할이나 담당 배정이 바뀌었을 수 있습니다. 권한이 돌아왔다면 [다시 확인]을 누르세요.';
export const SETTINGS_NO_ACCESS = '이 계정으로는 이 고객의 설정을 볼 수 없습니다. 역할이나 담당 배정이 바뀌었을 수 있습니다.';
export const SETTINGS_READ_ONLY =
  '이 계정은 설정을 볼 수만 있습니다. 변경은 고객사 ADMIN과 이 고객을 담당하는 플랫폼 운영자가 합니다.';
