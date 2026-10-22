// 새 비밀번호 정책 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CONFIRM_MISMATCH,
  PASSWORD_SYMBOLS,
  POLICY_UNMET,
  evaluatePassword,
  newPasswordError,
  policyMet,
} from './password-policy.ts';

/** 대 · 소 · 숫자 · 기호를 하나씩 담고 나머지를 소문자로 채운 길이 n(코드 포인트)의 비밀번호. */
const sized = (n: number) => `Aa1!${'a'.repeat(n - 4)}`;

test('허용 기호는 공백을 뺀 ASCII 문장부호 32자 전부다', () => {
  // 기대값은 ASCII 표에서 따로 만든다: 0x21–0x7E 중 영문 · 숫자가 아닌 것.
  const expected = new Set<string>();
  for (let code = 0x21; code <= 0x7e; code += 1) {
    const c = String.fromCharCode(code);
    if (!/[A-Za-z0-9]/.test(c)) expected.add(c);
  }
  assert.equal(PASSWORD_SYMBOLS.length, 32);
  assert.deepEqual(new Set(PASSWORD_SYMBOLS), expected);
  assert.equal(PASSWORD_SYMBOLS.includes(' '), false);
});

test('허용 기호 하나하나가 기호 조건을 채운다', () => {
  for (const symbol of PASSWORD_SYMBOLS) {
    const { checks, hasForbidden } = evaluatePassword(`Abcdefghij1${symbol}`, '');
    assert.equal(policyMet(checks), true, symbol);
    assert.equal(hasForbidden, false, symbol);
  }
});

test('길이는 12–256자(코드 포인트)다', () => {
  assert.equal(evaluatePassword(sized(11), '').checks.length, false);
  assert.equal(evaluatePassword(sized(12), '').checks.length, true);
  assert.equal(evaluatePassword(sized(256), '').checks.length, true);
  assert.equal(evaluatePassword(sized(257), '').checks.length, false);
  // 이모지는 UTF-16으로 2칸이지만 1자로 센다(서버 길이 검사와 같은 단위). 허용 문자는 아니다.
  const emoji = evaluatePassword(`${sized(11)}😀`, '');
  assert.equal(emoji.checks.length, true);
  assert.equal(emoji.checks.allowedOnly, false);
});

test('대문자 · 소문자 · 숫자 · 기호 조건은 각각 따로 판정한다', () => {
  assert.equal(evaluatePassword('aa1!aaaaaaaa', '').checks.upper, false);
  assert.equal(evaluatePassword('AA1!AAAAAAAA', '').checks.lower, false);
  assert.equal(evaluatePassword('Aa!!aaaaaaaa', '').checks.digit, false);
  assert.equal(evaluatePassword('Aa11aaaaaaaa', '').checks.symbol, false);
  assert.equal(policyMet(evaluatePassword('Aa1!aaaaaaaa', '').checks), true);
});

test('공백은 위치와 관계없이 허용되지 않는다 — 앞뒤를 지우지 않는다', () => {
  for (const pw of [' Aa1!aaaaaaaa', 'Aa1!aaaaaaaa ', 'Aa1! aaaaaaa', 'Aa1!\taaaaaaa', 'Aa1!\u00a0aaaaaaa']) {
    const result = evaluatePassword(pw, pw);
    assert.equal(result.checks.allowedOnly, false, JSON.stringify(pw));
    assert.equal(result.hasForbidden, true, JSON.stringify(pw));
    assert.equal(newPasswordError(pw, pw), POLICY_UNMET);
  }
});

test('한글 · 전각 · 악센트 문자는 허용되지 않고, 대 · 소문자로 세지도 않는다', () => {
  for (const extra of ['가', 'ㅎ', 'é', '！']) {
    assert.equal(evaluatePassword(`Aa1!aaaaaaaa${extra}`, '').hasForbidden, true, extra);
  }
  const fullwidth = evaluatePassword('Ａａ1!aaaaaaaa', '').checks;
  assert.equal(fullwidth.upper, false);
});

test('빈 값은 허용 문자 조건도 충족하지 않는다', () => {
  const { checks, hasForbidden, matches } = evaluatePassword('', '');
  assert.equal(Object.values(checks).some(Boolean), false);
  assert.equal(hasForbidden, false);
  assert.equal(matches, false);
});

test('확인 칸은 정확히 같아야 한다 — 정규화 · trim 없이 비교', () => {
  const pw = 'Aa1!aaaaaaaa';
  assert.equal(newPasswordError(pw, pw), null);
  assert.equal(newPasswordError(pw, ''), CONFIRM_MISMATCH);
  assert.equal(newPasswordError(pw, `${pw} `), CONFIRM_MISMATCH);
  assert.equal(newPasswordError(pw, 'aA1!aaaaaaaa'), CONFIRM_MISMATCH);
  // 조합형과 분해형은 다른 값이다(NFC로 맞추지 않는다).
  assert.equal(evaluatePassword('é', 'e\u0301').matches, false);
});

test('확인 칸의 허용되지 않은 문자도 따로 알린다 — 새 비밀번호가 맞아도', () => {
  const pw = 'Aa1!aaaaaaaa';
  for (const confirm of ['Aa1!aaaa가aaa', `${pw} `, 'Aa1! aaaaaaa', 'Aa1!\u00a0aaaaaaa']) {
    const result = evaluatePassword(pw, confirm);
    assert.equal(policyMet(result.checks), true, JSON.stringify(confirm));
    assert.equal(result.hasForbidden, false, JSON.stringify(confirm));
    assert.equal(result.confirmHasForbidden, true, JSON.stringify(confirm));
    assert.equal(result.matches, false, JSON.stringify(confirm));
    // 확인 칸 값도 다듬지 않는다 — 공백을 지워 같다고 보지 않는다.
    assert.equal(newPasswordError(pw, confirm), CONFIRM_MISMATCH);
    assert.equal(JSON.stringify(result).includes(confirm), false);
  }
  const ok = evaluatePassword(pw, pw);
  assert.equal(ok.confirmHasForbidden, false);
  assert.equal(evaluatePassword(pw, '').confirmHasForbidden, false);
});

test('정책 위반이 확인 불일치보다 먼저다', () => {
  assert.equal(newPasswordError('short', 'other'), POLICY_UNMET);
});

test('판정 결과에는 비밀번호가 실리지 않는다', () => {
  const pw = 'Secret#Value123';
  assert.equal(JSON.stringify(evaluatePassword(pw, pw)).includes(pw), false);
});
