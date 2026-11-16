// GuardDuty를 켜지 못했을 때의 안내 규칙(#588 D): 리전 목록 꺼내기 · 사유별 문구 · 고객사 안내문.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  GUARDDUTY_FEE_NOTICE,
  GUARDDUTY_SELF_ENABLE_NOTICE,
  guardDutyFailures,
  guardDutyMessage,
  guardDutyReason,
} from './guardduty.ts';
import { GUARDDUTY_GUIDE_PATH, PUBLIC_PATHS } from './return-to.ts';
import type { TenantVerification } from '../types/api.ts';

const AT = '2026-11-12T01:00:00Z';
const FAILURES = [
  { region: 'ap-northeast-2', reason_code: 'DetectorDisabled' },
  { region: 'us-east-1', reason_code: 'AccessDeniedException' },
];
const disabled = (guardduty_failures: TenantVerification['guardduty_failures']): TenantVerification => ({
  result: 'GUARDDUTY_DISABLED',
  verified_at: AT,
  reason_code: 'DetectorDisabled',
  guardduty_failures,
});
const TENANT = { aws_account_id: '123456789012', regions: ['ap-northeast-2', 'us-east-1', 'eu-west-1'] };
const GUIDE = `https://app.example${GUARDDUTY_GUIDE_PATH}`;

test('켜지 못한 리전 목록은 GUARDDUTY_DISABLED에서만 나오고, 목록 없는 예전 기록은 빈 배열이다', () => {
  assert.deepEqual(guardDutyFailures(disabled(FAILURES)), FAILURES);
  assert.deepEqual(guardDutyFailures(disabled(null)), []);
  assert.deepEqual(guardDutyFailures(null), []);
  assert.deepEqual(
    guardDutyFailures({ result: 'READ_DENIED', verified_at: AT, reason_code: 'AccessDenied', guardduty_failures: null }),
    [],
  );
});

test('사유 코드마다 할 일이 다르고, 모르는 코드는 「직접 켠다」로 접는다', () => {
  assert.match(guardDutyReason('DetectorDisabled').action, /다시 활성화/);
  // 정지된 탐지기는 플랫폼이 다시 켜지 않는다(#588 Q1) — 고객사가 그 사실을 알아야 한다
  assert.match(guardDutyReason('DetectorDisabled').action, /다시 켜지 않습니다/);
  assert.match(guardDutyReason('AccessDeniedException').action, /SCP/);
  // 권한 없는 예전 템플릿으로 만든 역할은 스택 업데이트가 푸는 길이다
  assert.match(guardDutyReason('AccessDeniedException').action, /스택을 업데이트/);
  assert.match(guardDutyReason('AccessDenied').action, /서비스 연결 역할/);
  assert.match(guardDutyReason('BadRequestException').action, /관리 계정/);
  const labels = ['DetectorDisabled', 'AccessDeniedException', 'AccessDenied', 'BadRequestException'].map(
    (code) => guardDutyReason(code).label,
  );
  assert.equal(new Set(labels).size, labels.length);

  const unknown = guardDutyReason('SomethingNewException');
  assert.equal(unknown.label, '켜지 못함');
  assert.match(unknown.action, /직접 켭니다/);
});

test('사유 코드가 객체의 기본 속성 이름이어도 모르는 코드로 다룬다', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    assert.equal(guardDutyReason(code).label, '켜지 못함', code);
  }
});

test('안내문은 켜지 못한 리전만 사유 · 할 일과 함께 싣고, 안내 페이지 주소와 요금 고지를 싣는다', () => {
  const message = guardDutyMessage(TENANT, FAILURES, GUIDE);
  const lines = message.split('\n');

  assert.match(lines[0], /AWS 계정 123456789012/);
  assert.deepEqual(
    lines.filter((line) => line.startsWith('- ')).map((line) => line.split(':')[0]),
    ['- ap-northeast-2', '- us-east-1'],
  );
  // 켜져 있던 관제 리전은 싣지 않는다 — 고객사가 켤 것만 보인다
  assert.doesNotMatch(message, /eu-west-1/);
  assert.match(message, /ap-northeast-2: 탐지기 정지됨\(DetectorDisabled\)\. .*다시 활성화/);
  assert.match(message, /us-east-1: 권한 거절\(AccessDeniedException\)\. .*SCP/);
  assert.ok(lines.includes(`켜는 방법: ${GUIDE}`));
  assert.ok(lines.includes(GUARDDUTY_FEE_NOTICE));
  // 받는 사람이 직접 켜는 사람이다 — 부가 보호 기능의 요금 주의를 링크 너머에 두지 않고 요금 고지 바로 뒤에 싣는다
  assert.equal(lines[lines.indexOf(GUARDDUTY_FEE_NOTICE) + 1], GUARDDUTY_SELF_ENABLE_NOTICE);
});

test('리전 목록이 없는 예전 기록의 안내문은 관제 리전 전부를 확인해 달라고 한다', () => {
  const message = guardDutyMessage(TENANT, [], GUIDE);
  assert.deepEqual(
    message.split('\n').filter((line) => line.startsWith('- ')),
    TENANT.regions.map((region) => `- ${region}: GuardDuty가 켜져 있는지 확인합니다.`),
  );
  // 리전 목록이 없어도 받는 사람은 직접 켜는 사람이다
  assert.ok(message.split('\n').includes(GUARDDUTY_SELF_ENABLE_NOTICE));
});

test('요금 고지는 고객 계정 부과와 계정 · 리전마다 30일 무료 체험을 말한다', () => {
  assert.match(GUARDDUTY_FEE_NOTICE, /고객사 AWS 계정에 부과/);
  assert.match(GUARDDUTY_FEE_NOTICE, /계정과 리전마다 처음 30일은 무료 체험/);
});

test('직접 켤 때의 주의는 부가 보호 기능이 함께 켜질 수 있다는 것과 따로 붙는 요금, 끄는 자리를 말한다', () => {
  // 고객사가 직접 켜면 플랫폼과 달리 부가 기능이 켜질 수 있다(#624 리뷰) — 요금 고지가 기본 탐지만 말하므로 따로 알린다
  assert.match(GUARDDUTY_SELF_ENABLE_NOTICE, /부가 보호 기능이 함께 켜질 수 있고/);
  assert.match(GUARDDUTY_SELF_ENABLE_NOTICE, /켜진 기능마다 요금이 따로/);
  assert.match(GUARDDUTY_SELF_ENABLE_NOTICE, /보호 플랜 설정에서 끕니다/);
});

test('안내 페이지는 고객사의 AWS 담당자가 로그인 없이 연다', () => {
  assert.ok(PUBLIC_PATHS.has(GUARDDUTY_GUIDE_PATH));
});
