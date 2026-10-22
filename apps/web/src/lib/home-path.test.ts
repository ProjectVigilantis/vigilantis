import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ADMIN_HOME, CUSTOMER_HOME, homePathFor } from './home-path.ts';

test('관리자는 고객 관리, 그 밖의 역할은 메인 대시보드로 간다', () => {
  assert.equal(homePathFor({ role: 'ADMIN' }), ADMIN_HOME);
  assert.equal(homePathFor({ role: 'OPERATOR' }), CUSTOMER_HOME);
  assert.equal(homePathFor({ role: 'VIEWER' }), CUSTOMER_HOME);
});
