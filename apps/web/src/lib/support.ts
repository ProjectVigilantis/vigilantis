// 도입 신청 · 고객 문의 · 관리자 알림의 표기와 이동 경로(2026-10-29). 계약은 초안이다(types/api.ts 끝).
//
// 도입 신청: 로그인 창 회원가입에서 처음 쓰는 회사가 보낸다. 운영자가 검토 → 승인(고객사 추가 · 관리자 초대로 이어감) · 반려.
// 고객 문의: 고객사 계정(관리자 · 직원 모두)이 장애 · 버그를 알린다. 운영자가 처리 중 → 해결로 옮긴다.
// 알림: 둘이 들어오면 관리자 상단바 종에 쌓인다. 누르면 해당 목록 화면에서 그 건을 펼친다(`?id=`).

import type {
  InquiryCategory,
  InquiryEventItem,
  InquiryStatus,
  NotificationItem,
  SignupRequestStatus,
} from '../types/api.ts';
import type { EnumLabel } from './enum-labels.ts';

/** 도입 신청함 · 고객 문의함(플랫폼 계정 전용 화면). */
export const SIGNUP_REQUESTS_PATH = '/signup-requests';
export const INQUIRIES_PATH = '/inquiries';

export const SIGNUP_STATUS_LABELS: Record<SignupRequestStatus, EnumLabel> = {
  NEW: { label: '새 신청', tone: 'yellow' },
  IN_REVIEW: { label: '검토 중', tone: 'blue' },
  APPROVED: { label: '승인', tone: 'green' },
  REJECTED: { label: '반려', tone: 'gray' },
};

export const INQUIRY_CATEGORY_LABELS: Record<InquiryCategory, EnumLabel> = {
  OUTAGE: { label: '장애', tone: 'red' },
  BUG: { label: '버그', tone: 'orange' },
  OTHER: { label: '기타', tone: 'neutral' },
};

/** 문의 종류를 고르는 자리의 설명. 고객이 어느 쪽인지 고를 수 있게 증상으로 적는다. */
export const INQUIRY_CATEGORY_HINTS: Record<InquiryCategory, string> = {
  OUTAGE: '화면이 열리지 않거나 관제 · 조치가 멈췄습니다',
  BUG: '동작은 하지만 값이나 결과가 틀립니다',
  OTHER: '사용 방법 · 계정 · 그 밖의 문의',
};

export const INQUIRY_STATUS_LABELS: Record<InquiryStatus, EnumLabel> = {
  OPEN: { label: '접수', tone: 'yellow' },
  IN_PROGRESS: { label: '처리 중', tone: 'blue' },
  RESOLVED: { label: '해결', tone: 'green' },
};

/** 운영자가 다음으로 옮길 수 있는 상태. 끝난 상태(승인 · 반려 · 해결)는 되돌리지 않는다. */
export const SIGNUP_NEXT: Record<SignupRequestStatus, SignupRequestStatus[]> = {
  NEW: ['IN_REVIEW', 'APPROVED', 'REJECTED'],
  IN_REVIEW: ['APPROVED', 'REJECTED'],
  APPROVED: [],
  REJECTED: [],
};

export const INQUIRY_NEXT: Record<InquiryStatus, InquiryStatus[]> = {
  OPEN: ['IN_PROGRESS', 'RESOLVED'],
  IN_PROGRESS: ['RESOLVED'],
  RESOLVED: [],
};

/** 처리 기록 한 줄의 제목. 상태 변경은 "접수 → 처리 중"처럼 앞뒤를 함께 적는다. */
export function inquiryEventTitle(event: InquiryEventItem): string {
  if (event.action === 'INQUIRY_CREATED') return '문의 접수';
  if (event.action === 'NOTE_ADDED') return '메모';
  const label = (s: InquiryStatus | null) => (s === null ? '알 수 없음' : INQUIRY_STATUS_LABELS[s].label);
  return `${label(event.before_status)} → ${label(event.after_status)}`;
}

/** 알림 한 줄의 글. 제목은 무엇이 들어왔는지, 설명은 누가 무엇을. */
export function notificationText(item: NotificationItem): { title: string; detail: string } {
  if (item.kind === 'SIGNUP_REQUEST') {
    return { title: `도입 신청 · ${item.subject}`, detail: `담당자 ${item.summary}` };
  }
  const category = item.inquiry_category === null ? '' : `${INQUIRY_CATEGORY_LABELS[item.inquiry_category].label} `;
  return { title: `${category}문의 · ${item.subject}`, detail: item.summary };
}

/** 알림을 누르면 갈 곳. 목록 화면이 `?id=`의 건을 펼친다. */
export function notificationHref(item: NotificationItem): string {
  const base = item.kind === 'SIGNUP_REQUEST' ? SIGNUP_REQUESTS_PATH : INQUIRIES_PATH;
  return `${base}?id=${encodeURIComponent(item.target_id)}`;
}

/** 서버가 아직 이 기능을 열지 않았나(경로 없음 404 · 메서드 없음 405). 화면은 오류 대신 준비 중으로 읽는다. */
export function notYetServed(httpStatus: number | null): boolean {
  return httpStatus === 404 || httpStatus === 405;
}
