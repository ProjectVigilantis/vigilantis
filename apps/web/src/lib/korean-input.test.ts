// 한글 입력 판정 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { containsHangul } from './korean-input.ts';

test('완성형 · 호환 자모 · 조합용 자모 · 반각 자모가 있으면 true', () => {
  for (const value of ['홍길동@example.com', 'passㅎ', 'a\u1112b', 'x\uFFA1', '\uA960', '\uD7B0']) {
    assert.equal(containsHangul(value), true, value);
  }
});

test('한글이 없으면 false — 다른 IME 문자도 경고하지 않는다', () => {
  for (const value of ['', 'user@example.com', 'Aa1!aaaaaaaa', '日本語', 'こんにちは', 'é', '！']) {
    assert.equal(containsHangul(value), false, value);
  }
});
