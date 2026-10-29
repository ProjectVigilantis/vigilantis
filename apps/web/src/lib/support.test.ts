import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { InquiryEventItem, NotificationItem } from '../types/api.ts';
import {
  INQUIRY_NEXT,
  inquiryEventTitle,
  SIGNUP_NEXT,
  notYetServed,
  notificationHref,
  notificationText,
} from './support.ts';

const base: NotificationItem = {
  notification_id: 'n1',
  kind: 'SIGNUP_REQUEST',
  target_id: 'sr 1',
  subject: '이음물류',
  summary: '김담당',
  inquiry_category: null,
  created_at: '2026-10-29T01:00:00Z',
  read_at: null,
};

test('도입 신청 알림은 회사명과 담당자를 보여 주고 신청함의 그 건으로 간다', () => {
  assert.deepEqual(notificationText(base), { title: '도입 신청 · 이음물류', detail: '담당자 김담당' });
  assert.equal(notificationHref(base), '/signup-requests?id=sr%201');
});

test('문의 알림은 종류 · 고객사 · 제목을 보여 주고 문의함의 그 건으로 간다', () => {
  const item: NotificationItem = { ...base, kind: 'INQUIRY', target_id: 'q1', summary: '관제 화면이 안 열림', inquiry_category: 'OUTAGE' };
  assert.deepEqual(notificationText(item), { title: '장애 문의 · 이음물류', detail: '관제 화면이 안 열림' });
  assert.equal(notificationHref(item), '/inquiries?id=q1');
});

test('종류를 모르는 문의 알림은 "문의"로만 적는다', () => {
  const item: NotificationItem = { ...base, kind: 'INQUIRY', inquiry_category: null };
  assert.equal(notificationText(item).title, '문의 · 이음물류');
});

test('끝난 상태(승인 · 반려 · 해결)에서는 더 옮길 곳이 없다', () => {
  assert.deepEqual(SIGNUP_NEXT.APPROVED, []);
  assert.deepEqual(SIGNUP_NEXT.REJECTED, []);
  assert.deepEqual(INQUIRY_NEXT.RESOLVED, []);
  assert.deepEqual(INQUIRY_NEXT.OPEN, ['IN_PROGRESS', 'RESOLVED']);
});

test('처리 기록 제목: 접수 · 상태 변경(앞 → 뒤) · 메모', () => {
  const event: InquiryEventItem = {
    event_id: 'e1',
    inquiry_id: 'q1',
    actor_user_id: 'u1',
    actor_email: 'admin@example.com',
    action: 'STATUS_CHANGED',
    before_status: 'OPEN',
    after_status: 'IN_PROGRESS',
    note: null,
    occurred_at: '2026-10-29T01:00:00Z',
  };
  assert.equal(inquiryEventTitle(event), '접수 → 처리 중');
  assert.equal(inquiryEventTitle({ ...event, action: 'INQUIRY_CREATED' }), '문의 접수');
  assert.equal(inquiryEventTitle({ ...event, action: 'NOTE_ADDED' }), '메모');
});

test('경로가 없는 응답(404 · 405)만 준비 중으로 읽는다', () => {
  assert.equal(notYetServed(404), true);
  assert.equal(notYetServed(405), true);
  assert.equal(notYetServed(500), false);
  assert.equal(notYetServed(null), false);
});
