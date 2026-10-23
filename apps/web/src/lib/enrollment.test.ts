// 플랫폼 초대 링크 판독 · 주소 정리 · 링크 상태 · 실패 문구 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ENROLLMENT_DONE,
  ENROLLMENT_LINK_INVALID,
  ENROLLMENT_UNCERTAIN,
  createEnrollmentLinkStore,
  enrollmentErrorView,
  parseEnrollmentFragment,
  settleEnrollment,
  takeEnrollmentLink,
  ENROLLMENT_EMAIL_EXISTS,
  ENROLLMENT_RATE_LIMITED,
  type EnrollmentLink,
} from './enrollment.ts';
import type { ErrorCode } from '../types/api.ts';

const TOKEN = 'Abc_DEF-ghi.jkl~MNOpqrstuvwxyz0123456789';

test('조각 #token=<값> 하나만 토큰으로 읽는다', () => {
  assert.equal(parseEnrollmentFragment(`#token=${TOKEN}`), TOKEN);
  assert.equal(parseEnrollmentFragment(`#token=${'a'.repeat(16)}`), 'a'.repeat(16));
  assert.equal(parseEnrollmentFragment(`#token=${'a'.repeat(512)}`), 'a'.repeat(512));
});

test('빈 · 짧은 · 긴 · 허용 밖 글자 · 다른 키 · 추가 값은 형식 밖이다', () => {
  for (const hash of [
    '',
    '#',
    '#token=',
    `#token=${'a'.repeat(15)}`,
    `#token=${'a'.repeat(513)}`,
    `#tok=${TOKEN}`,
    `#Token=${TOKEN}`,
    `token=${TOKEN}`,
    `#token=${TOKEN}&next=/x`,
    `#token=${TOKEN} `,
    `#token= ${TOKEN}`,
    `#token=${TOKEN}%20`,
    `#token=${TOKEN}+x`,
    `#token=${TOKEN}/x`,
    `#token=${TOKEN}#x`,
    `#a=1&token=${TOKEN}`,
  ]) {
    assert.equal(parseEnrollmentFragment(hash), null, JSON.stringify(hash));
  }
});

function fakeHistory() {
  const calls: unknown[][] = [];
  return { calls, replaceState: (...args: unknown[]) => void calls.push(args) };
}

test('토큰을 읽으면 이동 없이 경로 · 쿼리만 남기고 조각을 지운다', () => {
  const history = fakeHistory();
  const link = takeEnrollmentLink({ hash: `#token=${TOKEN}`, pathname: '/team/enroll', search: '?utm=x' }, history);
  assert.deepEqual(link, { kind: 'ready', token: TOKEN });
  assert.deepEqual(history.calls, [[null, '', '/team/enroll?utm=x']]);
  // 바꿔 쓴 주소에는 토큰이 없다.
  assert.doesNotMatch(String(history.calls[0][2]), new RegExp(TOKEN));
});

test('형식이 틀린 조각도 지우고, 토큰으로 쓰지 않는다', () => {
  const history = fakeHistory();
  const link = takeEnrollmentLink({ hash: `#token=${TOKEN}&x=1`, pathname: '/team/enroll', search: '' }, history);
  assert.deepEqual(link, { kind: 'invalid' });
  assert.deepEqual(history.calls, [[null, '', '/team/enroll']]);
});

test('조각이 없으면 invalid이고 주소를 건드리지 않는다', () => {
  const history = fakeHistory();
  assert.deepEqual(takeEnrollmentLink({ hash: '', pathname: '/team/enroll', search: '' }, history), { kind: 'invalid' });
  assert.equal(history.calls.length, 0);
});

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test('저장소 — 첫 구독에서 한 번만 읽고, StrictMode 구독 → 해지 → 재구독에도 토큰을 지킨다', async () => {
  let reads = 0;
  const store = createEnrollmentLinkStore(() => {
    reads += 1;
    return { kind: 'ready', token: TOKEN };
  });
  // 서버 렌더 · 하이드레이션은 pending — 토큰을 렌더 결과에 싣지 않는다.
  assert.deepEqual(store.getServerSnapshot(), { kind: 'pending' });
  assert.deepEqual(store.getSnapshot(), { kind: 'pending' });

  let notified = 0;
  const listener = () => (notified += 1);
  const off = store.subscribe(listener);
  assert.equal(reads, 1);
  assert.equal(notified, 1);
  const first = store.getSnapshot();
  assert.deepEqual(first, { kind: 'ready', token: TOKEN });

  // StrictMode: 같은 커밋 안에서 해지 후 곧바로 재구독한다.
  off();
  const offAgain = store.subscribe(listener);
  await tick();
  assert.equal(reads, 1, '조각을 다시 읽지 않는다(이미 지워졌다)');
  assert.equal(store.getSnapshot(), first, '같은 스냅숏 — 재렌더 루프가 없다');
  offAgain();
});

test('저장소 — 화면을 떠나면 토큰을 버리고, 다시 들어오면 그때 주소를 새로 읽는다', async () => {
  const links: EnrollmentLink[] = [{ kind: 'ready', token: TOKEN }, { kind: 'invalid' }];
  const store = createEnrollmentLinkStore(() => links.shift() ?? { kind: 'invalid' });
  const off = store.subscribe(() => {});
  assert.equal(store.getSnapshot().kind, 'ready');
  off();
  await tick();
  assert.deepEqual(store.getSnapshot(), { kind: 'pending' });
  store.subscribe(() => {});
  assert.deepEqual(store.getSnapshot(), { kind: 'invalid' });
});

test('저장소 — spend는 토큰을 버리고 다시 읽지 않는다', () => {
  let reads = 0;
  const store = createEnrollmentLinkStore(() => {
    reads += 1;
    return { kind: 'ready', token: TOKEN };
  });
  store.subscribe(() => {});
  store.spend('other_token_0123456789');
  assert.deepEqual(store.getSnapshot(), { kind: 'ready', token: TOKEN }, '다른 토큰으로는 버리지 않는다');
  store.spend(TOKEN);
  assert.deepEqual(store.getSnapshot(), { kind: 'spent' });
  assert.equal(JSON.stringify(store.getSnapshot()).includes(TOKEN), false);
  store.subscribe(() => {});
  assert.equal(reads, 1);
});

const TOKEN_B = 'Second_invitation_token_9876543210';

/** 초대 A로 들어와 요청을 보낸 뒤 화면을 떠나고(해지 · 한 틱), 초대 B로 다시 들어온 상태. */
async function revisitedWithB() {
  const links: EnrollmentLink[] = [{ kind: 'ready', token: TOKEN }, { kind: 'ready', token: TOKEN_B }];
  const store = createEnrollmentLinkStore(() => links.shift() ?? { kind: 'invalid' });
  const offA = store.subscribe(() => {});
  offA();
  await tick();
  store.subscribe(() => {});
  assert.deepEqual(store.getSnapshot(), { kind: 'ready', token: TOKEN_B });
  return store;
}

test('늦게 끝난 초대 A의 성공은 초대 B를 버리지 않고 화면에 반영하지 않는다', async () => {
  const store = await revisitedWithB();
  assert.equal(settleEnrollment(store, TOKEN, null), false);
  assert.deepEqual(store.getSnapshot(), { kind: 'ready', token: TOKEN_B });
});

test('늦게 끝난 초대 A의 최종 실패(링크 거절 · 결과 불명)도 초대 B를 버리지 않고 화면에 반영하지 않는다', async () => {
  for (const code of ['ENROLLMENT_UNAVAILABLE', 'AUTH_PROVIDER_UNAVAILABLE', null] as const) {
    const store = await revisitedWithB();
    const view = enrollmentErrorView(code);
    assert.equal(view.final, true, String(code));
    assert.equal(settleEnrollment(store, TOKEN, view), false, String(code));
    assert.deepEqual(store.getSnapshot(), { kind: 'ready', token: TOKEN_B }, String(code));
  }
});

test('지금 초대의 응답 — 성공 · 최종 실패는 토큰을 버리고, 확정 거절은 남겨 직접 다시 낼 수 있다', () => {
  const ready = () => {
    const store = createEnrollmentLinkStore(() => ({ kind: 'ready', token: TOKEN }));
    store.subscribe(() => {});
    return store;
  };
  for (const view of [null, enrollmentErrorView('ENROLLMENT_UNAVAILABLE'), enrollmentErrorView('INTERNAL_ERROR')]) {
    const store = ready();
    assert.equal(settleEnrollment(store, TOKEN, view), true);
    assert.deepEqual(store.getSnapshot(), { kind: 'spent' });
  }
  const store = ready();
  assert.equal(settleEnrollment(store, TOKEN, enrollmentErrorView('REQUEST_VALIDATION_FAILED')), true);
  assert.deepEqual(store.getSnapshot(), { kind: 'ready', token: TOKEN });
});

test('링크 거절(404)은 최종 — 더 요청하지 않고, 꺼짐 · 만료 · 사용됨을 가르지 않는 한 문구다', () => {
  const view = enrollmentErrorView('ENROLLMENT_UNAVAILABLE');
  assert.equal(view.final, true);
  assert.equal(view.uncertain, false);
  assert.match(view.message, /새 링크를 요청/);
});

test('예약 전 거절(422 · 403 출처)만 폼에 남아 사용자가 직접 다시 낸다', () => {
  for (const code of ['REQUEST_VALIDATION_FAILED', 'AUTH_ORIGIN_REJECTED'] as const) {
    const view = enrollmentErrorView(code);
    assert.equal(view.final, false, code);
    assert.equal(view.uncertain, false, code);
  }
});

const RESUBMIT = /다시 요청해|다시 요청하고|다른 이메일로|잠시 기다린 뒤|반복하기 전|메일이 없을 때만/;

test('409(예약 뒤 이메일 중복)은 최종 · 확정 — 새 계정을 만들지 않았고, 로그인 또는 새 링크 요청만 안내한다', () => {
  const view = enrollmentErrorView('USER_STATE_CONFLICT');
  assert.equal(view.final, true);
  assert.equal(view.uncertain, false);
  assert.equal(view.message, ENROLLMENT_EMAIL_EXISTS);
  assert.match(view.message, /이번 요청으로 새 계정을 만들지 않았습니다/);
  assert.match(view.message, /다시 쓸 수 없습니다/);
  assert.match(view.message, /로그인/);
  assert.match(view.message, /담당자에게 새 링크를 요청/);
  assert.doesNotMatch(view.message, RESUBMIT);
  // 404가 이유를 가르지 않으므로 링크 상태를 단정하는 추론을 싣지 않는다.
  assert.doesNotMatch(view.message, /사용 처리된 것/);
});

test('429(예약 뒤 인증 서비스 한도)는 최종 · 결과 불명 — 재요청 · 대기 후 재시도를 권하지 않는다', () => {
  const view = enrollmentErrorView('AUTH_RATE_LIMITED');
  assert.equal(view.final, true);
  assert.equal(view.uncertain, true);
  assert.equal(view.message, ENROLLMENT_RATE_LIMITED);
  assert.match(view.message, /만들어졌을 수 있습니다/);
  assert.match(view.message, /이 링크로는 다시 요청할 수 없습니다/);
  assert.match(view.message, /메일함을 확인/);
  assert.match(view.message, /담당자에게 문의/);
  assert.doesNotMatch(view.message, RESUBMIT);
});

test('지금 초대의 409 · 429 응답은 토큰을 버린다', () => {
  const token = TOKEN;
  for (const code of ['USER_STATE_CONFLICT', 'AUTH_RATE_LIMITED'] as const) {
    const store = createEnrollmentLinkStore(() => ({ kind: 'ready', token }));
    store.subscribe(() => {});
    assert.equal(settleEnrollment(store, TOKEN, enrollmentErrorView(code)), true, code);
    assert.deepEqual(store.getSnapshot(), { kind: 'spent' }, code);
  }
});

test('결과 불명(인증 서비스 · 메일 발송 · 500 · 네트워크 · 계약 밖)은 최종 — 재요청을 권하지 않고 메일함 확인 · 담당자 문의', () => {
  const unknown = 'SOMETHING_NEW' as ErrorCode;
  for (const code of ['AUTH_PROVIDER_UNAVAILABLE', 'AUTH_EMAIL_DELIVERY_FAILED', 'INTERNAL_ERROR', unknown, null] as const) {
    const view = enrollmentErrorView(code);
    assert.equal(view.uncertain, true, String(code));
    // 서버가 예약을 유지한다 — 이 링크로 더 보내지 않는다(토큰을 버리고 폼을 다시 열지 않는다).
    assert.equal(view.final, true, String(code));
    assert.match(view.message, /메일함을 확인/, String(code));
    assert.match(view.message, /담당자에게 문의/, String(code));
    assert.match(view.message, /이 링크로는 다시 요청할 수 없습니다/, String(code));
    // 메일이 없다는 것은 생성되지 않았다는 증거가 아니다 — 조건부 재요청 · 반복을 권하지 않는다.
    assert.doesNotMatch(view.message, /다시 요청해|다시 요청하고|반복하기 전|메일이 없을 때만/, String(code));
    assert.doesNotMatch(view.message, /만들지 못했|생성되지 않았|실패했습니다/, String(code));
  }
  assert.equal(enrollmentErrorView(null).message, ENROLLMENT_UNCERTAIN);
  assert.match(enrollmentErrorView('AUTH_EMAIL_DELIVERY_FAILED').message, /만들어졌을 수 있지만/);
});

test('성공 문구는 메일 도착 · 이메일 확인 · 로그인 완료를 말하지 않고 로그인 단계를 안내한다', () => {
  const done = ENROLLMENT_DONE.join(' ');
  assert.doesNotMatch(done, /도착했습니다|확인되었습니다|인증되었습니다|로그인되었습니다|발송했습니다/);
  assert.match(done, /발송을 요청했습니다/);
  assert.match(done, /아직 끝나지 않았습니다/);
  for (const step of ['새 비밀번호', 'TOTP', '이메일 인증', '로그인']) assert.ok(done.includes(step), step);
});

test('어떤 문구에도 토큰이 들어갈 자리가 없다(입력은 코드뿐)', () => {
  const codes: (ErrorCode | null)[] = [
    'ENROLLMENT_UNAVAILABLE', 'AUTH_RATE_LIMITED', 'USER_STATE_CONFLICT', 'REQUEST_VALIDATION_FAILED',
    'AUTH_ORIGIN_REJECTED', 'AUTH_EMAIL_DELIVERY_FAILED', 'AUTH_PROVIDER_UNAVAILABLE', 'INTERNAL_ERROR', null,
  ];
  for (const message of [...codes.map((c) => enrollmentErrorView(c).message), ENROLLMENT_LINK_INVALID, ...ENROLLMENT_DONE]) {
    assert.doesNotMatch(message, /token|토큰|#/i);
  }
});
