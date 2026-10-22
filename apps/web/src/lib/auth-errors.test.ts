// 로그인 오류 문구 · 재시작 판정 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  RESET_CONFIRM_UNCERTAIN,
  authErrorView,
  resetConfirmErrorMessage,
  resetRequestErrorMessage,
} from './auth-errors.ts';

test('재설정 실패 문구는 로그인 재시작을 말하지 않는다', () => {
  for (const code of ['AUTH_RATE_LIMITED', 'AUTH_PROVIDER_UNAVAILABLE', 'AUTH_EMAIL_DELIVERY_FAILED', null] as const) {
    assert.doesNotMatch(resetRequestErrorMessage(code), /처음부터/, String(code));
  }
  for (const code of ['AUTH_CODE_INVALID', 'AUTH_PASSWORD_POLICY', 'AUTH_RATE_LIMITED', 'INTERNAL_ERROR', null] as const) {
    assert.doesNotMatch(resetConfirmErrorMessage(code), /처음부터/, String(code));
  }
});

test('재설정 확인의 결과 불명 실패는 "바뀌지 않았다"고 말하지 않고 다음 행동을 안내한다', () => {
  for (const code of ['INTERNAL_ERROR', 'AUTH_PROVIDER_UNAVAILABLE', null] as const) {
    assert.equal(resetConfirmErrorMessage(code), RESET_CONFIRM_UNCERTAIN, String(code));
  }
  assert.doesNotMatch(RESET_CONFIRM_UNCERTAIN, /바뀌지 않았|변경되지 않았|실패했습니다/);
  assert.match(RESET_CONFIRM_UNCERTAIN, /새 비밀번호로 로그인/);
  assert.match(RESET_CONFIRM_UNCERTAIN, /재설정을 다시 요청/);
  assert.match(RESET_CONFIRM_UNCERTAIN, /관리자에게 문의/);
});

test('재설정 확인의 코드 · 정책 오류는 같은 화면에서 고칠 수 있게 안내한다', () => {
  assert.match(resetConfirmErrorMessage('AUTH_CODE_INVALID'), /코드/);
  assert.match(resetConfirmErrorMessage('AUTH_PASSWORD_POLICY'), /허용된 기호/);
});

test('로그인 진행이 무효가 된 코드만 비밀번호 단계로 되돌린다', () => {
  for (const code of ['AUTH_FLOW_EXPIRED', 'AUTH_STEP_CONFLICT', 'AUTH_REQUIRED'] as const) {
    assert.equal(authErrorView(code).restart, true, code);
    assert.match(authErrorView(code).message, /처음부터 다시 로그인/);
  }
  for (const code of ['AUTH_FAILED', 'AUTH_CODE_INVALID', 'AUTH_PASSWORD_POLICY'] as const) {
    assert.equal(authErrorView(code).restart, false, code);
  }
});

test('서버가 로그인 진행을 끝낸 일시 장애는 잠시 후 처음부터 다시 하게 한다', () => {
  for (const code of ['AUTH_RATE_LIMITED', 'AUTH_PROVIDER_UNAVAILABLE', 'AUTH_EMAIL_DELIVERY_FAILED'] as const) {
    assert.equal(authErrorView(code).restart, true, code);
    assert.match(authErrorView(code).message, /잠시 후 처음부터 다시 로그인/, code);
  }
});

test('코드별 문구', () => {
  assert.equal(authErrorView('AUTH_FAILED').message, '이메일 또는 비밀번호가 올바르지 않습니다.');
  assert.match(authErrorView('AUTH_CODE_INVALID').message, /코드가 일치하지 않습니다/);
  assert.match(authErrorView('AUTH_PASSWORD_POLICY').message, /12자 이상/);
  assert.match(authErrorView('AUTH_RATE_LIMITED').message, /시도가 너무 많습니다/);
  for (const code of ['ACCOUNT_NOT_PROVISIONED', 'ACCOUNT_DISABLED'] as const) {
    assert.match(authErrorView(code).message, /관리자에게 문의/, code);
  }
});

test('네트워크 실패와 계약 밖 코드는 같은 단계에 남는 일반 문구다', () => {
  assert.deepEqual(authErrorView(null), {
    message: '서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',
    restart: false,
  });
  assert.equal(authErrorView('INTERNAL_ERROR').restart, false);
  assert.match(authErrorView('INTERNAL_ERROR').message, /잠시 후 다시/);
});
