import assert from 'node:assert/strict';
import { test } from 'node:test';

import { threatDetail, threatKindCounts, threatSource, timelinePercent, type ThreatEventItem } from './threat-events.ts';

const ssh: ThreatEventItem = {
  threat_event_id: 't1',
  event_type: 'SSH_BRUTE_FORCE',
  target_arn: 'arn:aws:ec2:ap-northeast-2:310987654321:instance/i-1',
  occurred_at: '2026-11-03T03:00:00Z',
  payload: { source_ip: '203.0.113.7', failed_attempt_count: 42, window_seconds: 300 },
};
const open: ThreatEventItem = {
  threat_event_id: 't2',
  event_type: 'OPEN_IP',
  target_arn: 'arn:aws:ec2:ap-northeast-2:310987654321:security-group/sg-1',
  occurred_at: '2026-11-03T01:00:00Z',
  payload: { protocol: 'tcp', from_port: 22, to_port: 22, source_cidr: '0.0.0.0/0' },
};

test('출발지: SSH는 공격 IP, 개방 포트는 허용 대역으로 가른다', () => {
  assert.deepEqual(threatSource(ssh), { label: '출발지', value: '203.0.113.7' });
  assert.deepEqual(threatSource(open), { label: '허용 대역', value: '0.0.0.0/0' });
});

test('세부: 실패 횟수와 분, 프로토콜과 포트', () => {
  assert.equal(threatDetail(ssh), '5분 동안 로그인 실패 42회');
  assert.equal(threatDetail(open), 'TCP 22');
  assert.equal(threatDetail({ ...open, payload: { ...open.payload, from_port: 1000, to_port: 2000 } }), 'TCP 1000-2000');
  assert.equal(threatDetail({ ...open, payload: { ...open.payload, from_port: null, to_port: null } }), 'TCP 전체 포트');
});

test('세부: 분으로 나누어떨어지지 않는 구간은 초로, 한쪽 끝만 있는 포트는 단일 포트로 적는다', () => {
  assert.equal(threatDetail({ ...ssh, payload: { ...ssh.payload, window_seconds: 30 } }), '30초 동안 로그인 실패 42회');
  assert.equal(threatDetail({ ...ssh, payload: { ...ssh.payload, window_seconds: 90 } }), '90초 동안 로그인 실패 42회');
  assert.equal(threatDetail({ ...ssh, payload: { ...ssh.payload, window_seconds: 60 } }), '1분 동안 로그인 실패 42회');
  assert.equal(threatDetail({ ...open, payload: { ...open.payload, from_port: 22, to_port: null } }), 'TCP 22');
  assert.equal(threatDetail({ ...open, payload: { ...open.payload, from_port: null, to_port: 443 } }), 'TCP 443');
});

test('타임라인 위치는 기간 비율이고 기간 밖은 양 끝에 붙는다', () => {
  const s = '2026-11-02T04:00:00Z';
  const e = '2026-11-03T04:00:00Z';
  assert.equal(timelinePercent('2026-11-02T16:00:00Z', s, e), 50);
  assert.equal(timelinePercent('2026-11-01T00:00:00Z', s, e), 0);
  assert.equal(timelinePercent('2026-11-04T00:00:00Z', s, e), 100);
});

test('유형별 건수', () => {
  assert.deepEqual(threatKindCounts([ssh, ssh, open]), { SSH_BRUTE_FORCE: 2, OPEN_IP: 1, GUARDDUTY_FINDING: 0 });
});
