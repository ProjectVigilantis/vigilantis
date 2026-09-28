// AST-002 spec 값 모양 분기 회귀 — `npm test`.
//
// 지키는 것은 둘이다. ① 태그처럼 Key→Value 로 온 값이 `[object Object]`로 새지 않는다
// (SG·EC2 상세에서 실제로 그렇게 보이던 자리 — 서버 계약의 `Ec2Spec.tags`·`SgSpec.tags`)
// ② "값이 없다"는 `null`·빈 배열·빈 객체가 전부 같은 `EMPTY`다 — 빈 태그(`{}`)만 다르게
// 그리면 태그 없는 자산에서 다시 깨진 값이 보인다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { specValueView } from './spec-value.ts';

test('태그 맵은 키·값 쌍으로 펼친다 — 객체가 문자열로 새지 않는다', () => {
  const view = specValueView({ Name: 'vigilantis-seed-unused', 'vigilantis:seed': 'true' });

  assert.equal(view.kind, 'MAP');
  assert.deepEqual(view.kind === 'MAP' ? view.entries : null, [
    ['Name', 'vigilantis-seed-unused'],
    ['vigilantis:seed', 'true'],
  ]);
});

test('응답 순서를 그대로 둔다 — 화면이 서버가 준 순서로 읽힌다', () => {
  const view = specValueView({ b: '2', a: '1' });

  assert.deepEqual(view.kind === 'MAP' ? view.entries.map(([k]) => k) : null, ['b', 'a']);
});

test('빈 것 셋은 같은 EMPTY다 — null · 빈 배열 · 빈 객체', () => {
  for (const empty of [null, [], {}]) {
    assert.equal(specValueView(empty).kind, 'EMPTY', `${JSON.stringify(empty)}`);
  }
});

test('불리언·배열·스칼라 분기는 종전 그대로다', () => {
  assert.deepEqual(specValueView(false), { kind: 'BOOLEAN', value: false });
  assert.deepEqual(specValueView(['subnet-1']), { kind: 'LIST', items: ['subnet-1'] });
  assert.deepEqual(specValueView('t3.micro'), {
    kind: 'SCALAR',
    text: 't3.micro',
    numeric: false,
  });
});

test('숫자는 자릿수 정렬용으로 따로 표시한다 — 0도 스칼라다', () => {
  assert.deepEqual(specValueView(0), { kind: 'SCALAR', text: '0', numeric: true });
  assert.deepEqual(specValueView(8), { kind: 'SCALAR', text: '8', numeric: true });
});

test('포트 규칙 배열은 LIST로 남긴다 — 항목 판정은 컴포넌트 몫이다', () => {
  const rule = { protocol: 'tcp', from_port: 22, to_port: 22, ipv6: false };
  const view = specValueView([rule]);

  assert.equal(view.kind, 'LIST');
  assert.deepEqual(view.kind === 'LIST' ? view.items : null, [rule]);
});
