'use client';

// 고객 설정(T-7) — 보호 태그 · Slack · 이메일 알림. 고객사 화면(app/settings)과 플랫폼 고객 상세(app/tenants/[id]/settings)가
// 같은 폼을 쓴다. 최소 동작 화면이다(시각 정리는 FE 담당 몫).
//
// - 저장은 바뀐 것만 PATCH로 보낸다(lib/tenant-settings.ts `settingsUpdate`). 바뀐 것이 없으면 보내지 않는다.
// - 성공하면 응답으로 다시 그리고 새 Webhook URL 입력을 비운다. 실패하면 입력을 그대로 두고 오류를 보인다.
// - [상태 새로고침]은 GET으로 이메일 상태 등을 다시 읽되, 저장하지 않은 입력은 지우지 않는다(`rebaseDraft`).
// - 이메일 연결 실패(SETUP_FAILED)에는 [같은 주소로 다시 연결]이 저장 주소를 그대로 다시 보낸다(`emailRetryRequest`).
//   다른 칸의 저장하지 않은 입력은 그대로 둔다.
// - 폼은 확인된 계정에 이 고객 읽기 권한이 있을 때만 그린다(lib/tenant-settings.ts `settingsScope`). 쓰기 버튼은 쓰기
//   권한이 있을 때만이다. 계정 · 역할 · 배정이 바뀌어 범위 키가 바뀌면 폼을 새로 만들어 저장하지 않은 입력(Webhook URL
//   포함)을 버리고, 읽기 권한이 없어지면 폼을 내린다. 내려간 폼의 늦은 응답은 버린다.
// - 서버 거절은 요청 종류로 가른다(`settingsDenial`). 조회 거절(GET 403 · 어떤 요청이든 범위 밖 404)이면 `/me` 재조회
//   결과를 기다리지 않고 곧바로 폼과 저장값을 내리고(`settingsViewAfter`), 새로 성공한 조회로만 다시 그린다. 저장 403은
//   쓰기만 막힌 것이라 폼은 조회 전용으로 남는다. 어느 쪽이든 `/me`를 다시 읽어 화면 권한을 맞춘다. 실제 차단은 서버다.
// - 브라우저 이메일 검증은 끈다(`noValidate`) — 계약의 Email은 ASCII 밖 문자도 받으므로 판정은 `settingsUpdate`가 한다.
// - Webhook URL은 비밀값이다. 컴포넌트 상태에만 두고 저장 · 로그하지 않는다. 고객이 바뀌면 페이지가 `key`로 이
//   컴포넌트를 새로 만들어 이전 고객의 입력 · 응답이 넘어가지 않는다.

import { useEffect, useRef, useState, type FormEvent } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { ErrorState } from '@/components/error-state';
import { INPUT_CLASS, LABEL_CLASS } from '@/components/form-classes';
import { Panel } from '@/components/panel';
import { EnumBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { ApiError, getTenantSettings, updateTenantSettings } from '@/lib/api/client';
import {
  EMAIL_RETRY_STARTED,
  SETTINGS_ACCOUNT_PENDING,
  SETTINGS_NO_ACCESS,
  SETTINGS_READ_ONLY,
  SETTINGS_SAVED,
  SLACK_STATUS_LABELS,
  draftFrom,
  emailRetryRequest,
  emailStatusEntry,
  hasChanges,
  rebaseDraft,
  SETTINGS_READ_DENIED,
  settingsDenial,
  settingsScope,
  settingsUpdate,
  settingsViewAfter,
  type SettingsDraft,
  type SettingsErrors,
  type SettingsEvent,
  type SettingsOperation,
  type SettingsView,
} from '@/lib/tenant-settings';
import { formatKst } from '@/lib/utils';
import type { ProtectedTagRule, TenantSettings } from '@/types/api';

type Busy = 'save' | 'retry' | 'refresh' | null;

export function TenantSettingsView({ tenantId, initial }: { tenantId: string; initial: TenantSettings }) {
  const { session, refresh: refreshSession } = useSession();
  const user = session.kind === 'user' ? session.user : null;
  const scope = settingsScope(user, tenantId);
  // 마지막으로 받은 저장값(비밀값 없음 — 폼을 새로 만들어도 이어 쓴다) 또는 조회 거절. 읽기 권한이 있을 때만 그린다.
  const [view, setView] = useState<SettingsView>({ kind: 'ready', saved: initial });
  const onEvent = (event: SettingsEvent) => setView((current) => settingsViewAfter(current, event));

  if (user === null) {
    return <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">{SETTINGS_ACCOUNT_PENDING}</p>;
  }
  if (scope === null) {
    return <p role="alert" className="rounded-lg border p-4 text-sm text-muted-foreground">{SETTINGS_NO_ACCESS}</p>;
  }
  if (view.kind === 'denied') {
    return (
      <ReadDenied key={scope.key} tenantId={tenantId} error={view.error} onEvent={onEvent} refreshSession={refreshSession} />
    );
  }
  return (
    <SettingsForm
      key={scope.key}
      tenantId={tenantId}
      writable={scope.write}
      saved={view.saved}
      onEvent={onEvent}
      refreshSession={refreshSession}
    />
  );
}

/** 내려간 컴포넌트에 늦게 도착한 응답을 버리는 데 쓴다. */
function useMounted() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return mounted;
}

const codeOf = (error: unknown) => (error instanceof ApiError ? error.code : null);

/** 조회 거절 뒤의 안내. 저장값 · 입력은 이미 버렸다. [다시 확인]이 `/me`를 다시 읽고 설정 조회에 성공해야 폼이 돌아온다. */
function ReadDenied({
  tenantId,
  error,
  onEvent,
  refreshSession,
}: {
  tenantId: string;
  error: unknown;
  onEvent: (event: SettingsEvent) => void;
  refreshSession: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(error);
  const inFlight = useRef(false);
  const mounted = useMounted();

  async function recheck() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      // `/me`를 확인하지 못해도 조회는 시도한다 — 볼 수 있는지는 서버 응답이 정한다.
      await refreshSession().catch(() => null);
      // 재조회로 계정 · 범위가 바뀌었으면 이 안내는 새로 만들어졌다 — 낡은 범위로 조회하지 않는다.
      if (!mounted.current) return;
      const next = await getTenantSettings(tenantId);
      if (mounted.current) onEvent({ type: 'loaded', settings: next });
    } catch (caught) {
      if (mounted.current) setFailure(caught);
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p role="alert" className="rounded-lg border p-4 text-sm text-muted-foreground">{SETTINGS_READ_DENIED}</p>
      {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}
      <Button type="button" variant="outline" size="sm" className="w-fit" disabled={busy} onClick={recheck}>
        {busy ? '확인 중…' : '다시 확인'}
      </Button>
    </div>
  );
}

function SettingsForm({
  tenantId,
  writable,
  saved,
  onEvent,
  refreshSession,
}: {
  tenantId: string;
  writable: boolean;
  saved: TenantSettings;
  onEvent: (event: SettingsEvent) => void;
  refreshSession: () => Promise<unknown>;
}) {
  const [draft, setDraft] = useState<SettingsDraft>(() => draftFrom(saved));
  const [busy, setBusy] = useState<Busy>(null);
  const [errors, setErrors] = useState<SettingsErrors>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 서버가 저장을 거절했다(PATCH 403) — `/me` 재조회로 범위 키가 바뀔 때까지 조회 전용으로 둔다.
  const [writeDenied, setWriteDenied] = useState(false);
  const inFlight = useRef(false);
  const mounted = useMounted();

  const canWrite = writable && !writeDenied;
  const editable = canWrite && busy !== 'save';
  const dirty = hasChanges(saved, draft);
  const slackConfigured = saved.notifications.slack_configured;
  const email = emailStatusEntry(saved.notifications);
  // 관리 주소가 없을 때만 이전 방식 경로를 따로 다룬다(있으면 서버 불변식상 legacy가 아니다).
  const legacyEmail = saved.notifications.legacy_sns_configured && saved.notifications.email_status === 'UNSET';
  const retry = canWrite ? emailRetryRequest(saved) : null;

  /** 요청 하나를 보낸다. 다른 요청이 진행 중이면 건너뛴다. 폼이 내려간 뒤 도착한 응답은 버린다. */
  async function run(kind: Exclude<Busy, null>, call: () => Promise<TenantSettings>, apply: (next: TenantSettings) => void) {
    if (inFlight.current) return;
    const operation: SettingsOperation = kind === 'refresh' ? 'read' : 'write';
    inFlight.current = true;
    setBusy(kind);
    setFailure(null);
    setNotice(null);
    try {
      const next = await call();
      if (!mounted.current) return;
      onEvent({ type: 'loaded', settings: next });
      apply(next);
    } catch (error) {
      if (!mounted.current) return;
      const code = codeOf(error);
      const denial = settingsDenial(operation, code);
      setFailure(error);
      if (denial === 'write') setWriteDenied(true);
      // 조회 거절이면 이 호출로 폼이 내려간다(저장값 · Webhook 입력 포함) — `/me` 재조회의 성패와 무관하다.
      onEvent({ type: 'failed', operation, code, error });
      // 재조회 실패는 이미 보인 오류 외에 알리지 않는다. 다시 그리는 길은 새로 성공한 조회뿐이다.
      if (denial !== null) refreshSession().catch(() => {});
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(null);
    }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canWrite) return;
    const result = settingsUpdate(saved, draft);
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    const body = result.request;
    if (body === null) return;
    void run('save', () => updateTenantSettings(tenantId, body), (next) => {
      setDraft(draftFrom(next));
      setNotice(SETTINGS_SAVED);
    });
  }

  /** 응답(저장값은 `run`이 올린다) 위에 저장하지 않은 입력을 남긴다(새로고침 · 이메일 재연결). */
  function rebaseOnto(before: TenantSettings, message: string | null) {
    return (next: TenantSettings) => {
      setDraft((current) => rebaseDraft(current, before, next));
      setNotice(message);
    };
  }

  function reload() {
    void run('refresh', () => getTenantSettings(tenantId), rebaseOnto(saved, null));
  }

  function retryEmail() {
    if (retry === null) return;
    void run('retry', () => updateTenantSettings(tenantId, retry), rebaseOnto(saved, EMAIL_RETRY_STARTED));
  }

  const setTags = (update: (tags: ProtectedTagRule[]) => ProtectedTagRule[]) =>
    setDraft((current) => ({ ...current, tags: update(current.tags) }));
  const setRule = (index: number, update: (rule: ProtectedTagRule) => ProtectedTagRule) =>
    setTags((tags) => tags.map((rule, i) => (i === index ? update(rule) : rule)));

  return (
    <form onSubmit={save} noValidate className="flex flex-col gap-4">
      {!canWrite ? (
        <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">
          {SETTINGS_READ_ONLY}
        </p>
      ) : null}
      {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}
      {notice ? (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">
          {notice}
        </p>
      ) : null}

      <fieldset disabled={!editable} className="grid min-w-0 gap-4 lg:grid-cols-2">
        <Panel
          title="보호 태그"
          className="lg:col-span-2"
          description="규칙 하나라도 맞는 태그가 붙은 자산을 보호 대상으로 봅니다. 키는 대소문자를 가리지 않고, 값은 앞뒤 공백 · 대소문자를 가리지 않습니다. 저장한 규칙은 다음 평가부터 적용됩니다."
        >
          <div className="flex flex-col gap-3">
            {draft.tags.length === 0 ? (
              <p className="text-sm text-muted-foreground">보호 태그 규칙이 없습니다.</p>
            ) : (
              draft.tags.map((rule, i) => (
                <div key={i} className="flex flex-col gap-2 rounded-md border p-3">
                  <div className="flex items-end gap-2">
                    <label className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <span className={LABEL_CLASS}>{i + 1}번째 규칙 · 태그 키</span>
                      <input className={INPUT_CLASS} value={rule.key} autoComplete="off" spellCheck={false}
                        onChange={(event) => setRule(i, (r) => ({ ...r, key: event.target.value }))} />
                    </label>
                    {canWrite ? (
                      <Button type="button" variant="ghost" size="sm"
                        onClick={() => setTags((tags) => tags.filter((_, k) => k !== i))}>
                        규칙 삭제
                      </Button>
                    ) : null}
                  </div>
                  <span className="text-xs text-muted-foreground">값 — 하나라도 같으면 보호합니다. 빈 칸은 값이 비어 있는 태그입니다.</span>
                  {rule.values.map((value, j) => (
                    <div key={j} className="flex items-center gap-2">
                      <input className={INPUT_CLASS} value={value} autoComplete="off" spellCheck={false}
                        aria-label={`${i + 1}번째 규칙 값 ${j + 1}`}
                        onChange={(event) =>
                          setRule(i, (r) => ({ ...r, values: r.values.map((v, k) => (k === j ? event.target.value : v)) }))
                        } />
                      {canWrite ? (
                        <Button type="button" variant="ghost" size="sm" aria-label={`${i + 1}번째 규칙 값 ${j + 1} 삭제`}
                          onClick={() => setRule(i, (r) => ({ ...r, values: r.values.filter((_, k) => k !== j) }))}>
                          삭제
                        </Button>
                      ) : null}
                    </div>
                  ))}
                  {canWrite ? (
                    <Button type="button" variant="outline" size="sm" className="w-fit"
                      onClick={() => setRule(i, (r) => ({ ...r, values: [...r.values, ''] }))}>
                      값 추가
                    </Button>
                  ) : null}
                </div>
              ))
            )}
            {errors.tags ? <p className="text-xs text-destructive">{errors.tags}</p> : null}
            {canWrite ? (
              <div className="flex flex-wrap gap-2">
                <Button type="button" variant="outline" size="sm"
                  onClick={() => setTags((tags) => [...tags, { key: '', values: [''] }])}>
                  규칙 추가
                </Button>
                {draft.tags.length > 0 ? (
                  <Button type="button" variant="outline" size="sm" onClick={() => setTags(() => [])}>
                    전체 비우기
                  </Button>
                ) : null}
              </div>
            ) : null}
          </div>
        </Panel>

        <Panel title="Slack 알림" description="Slack 앱의 Incoming Webhook으로 알림을 보냅니다.">
          <div className="flex flex-col gap-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">상태</span>
              <EnumBadge entry={SLACK_STATUS_LABELS[slackConfigured ? 'configured' : 'unset']} />
            </div>
            {slackConfigured ? (
              <p className="text-xs text-muted-foreground">저장한 Webhook URL은 다시 보여 주지 않습니다. 바꾸려면 새 URL을 넣으세요.</p>
            ) : null}
            {draft.slackRemove ? (
              <p className="text-xs">저장하면 Slack 연결을 해제합니다.</p>
            ) : (
              <label className="flex flex-col gap-1.5">
                <span className={LABEL_CLASS}>{slackConfigured ? '새 Webhook URL' : 'Webhook URL'}</span>
                <input className={INPUT_CLASS} type="text" value={draft.slackUrl} autoComplete="off" spellCheck={false}
                  placeholder="https://hooks.slack.com/services/…" aria-invalid={errors.slack ? true : undefined}
                  onChange={(event) => setDraft((current) => ({ ...current, slackUrl: event.target.value }))} />
              </label>
            )}
            {errors.slack ? <p className="text-xs text-destructive">{errors.slack}</p> : null}
            {canWrite && slackConfigured ? (
              <Button type="button" variant="outline" size="sm" className="w-fit"
                onClick={() => setDraft((current) => ({ ...current, slackUrl: '', slackRemove: !current.slackRemove }))}>
                {draft.slackRemove ? '해제 취소' : '연결 해제'}
              </Button>
            ) : null}
          </div>
        </Panel>

        <Panel title="이메일 알림" description="주소를 등록하면 수신 구독 연결을 시작합니다. 구독 확인 메일이 도착하면 받는 사람이 확인해야 알림이 나갑니다.">
          <div className="flex flex-col gap-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">상태</span>
              <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                {saved.notifications.email_address ? (
                  <span className="font-mono text-xs break-all">{saved.notifications.email_address}</span>
                ) : null}
                <EnumBadge entry={email} />
              </span>
            </div>
            <p className="text-xs text-muted-foreground">{email.hint}</p>
            {retry !== null ? (
              <Button type="button" variant="outline" size="sm" className="w-fit" disabled={busy !== null} onClick={retryEmail}>
                {busy === 'retry' ? '다시 연결하는 중…' : '같은 주소로 다시 연결'}
              </Button>
            ) : null}
            {draft.legacyEmailRemove ? (
              <p className="text-xs">저장하면 기존 SNS 이메일 경로를 해제합니다.</p>
            ) : (
              <>
                <label className="flex flex-col gap-1.5">
                  <span className={LABEL_CLASS}>{legacyEmail ? '새 받는 주소' : '받는 주소'}</span>
                  <input className={INPUT_CLASS} type="email" value={draft.email} autoComplete="off" spellCheck={false}
                    aria-invalid={errors.email ? true : undefined}
                    onChange={(event) => setDraft((current) => ({ ...current, email: event.target.value }))} />
                </label>
                <p className="text-xs text-muted-foreground">
                  {legacyEmail
                    ? '새 주소를 저장하면 기존 경로 대신 그 주소로 수신 구독 연결을 시작합니다.'
                    : '비우고 저장하면 이메일 알림을 해제합니다. 주소를 바꾸면 새 주소로 수신 구독 연결을 다시 시작합니다.'}
                </p>
              </>
            )}
            {errors.email ? <p className="text-xs text-destructive">{errors.email}</p> : null}
            {canWrite && legacyEmail ? (
              <Button type="button" variant="outline" size="sm" className="w-fit"
                onClick={() => setDraft((current) => ({ ...current, email: '', legacyEmailRemove: !current.legacyEmailRemove }))}>
                {draft.legacyEmailRemove ? '해제 취소' : '기존 경로 해제'}
              </Button>
            ) : null}
          </div>
        </Panel>
      </fieldset>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          마지막 변경 {formatKst(saved.updated_at)}
          {dirty && canWrite ? ' · 저장하지 않은 변경이 있습니다' : ''}
        </span>
        <div className="flex gap-2">
          <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={reload}>
            {busy === 'refresh' ? '불러오는 중…' : '상태 새로고침'}
          </Button>
          {canWrite ? (
            <Button type="submit" size="sm" disabled={busy !== null || !dirty}>
              {busy === 'save' ? '저장하는 중…' : '저장'}
            </Button>
          ) : null}
        </div>
      </div>
    </form>
  );
}
