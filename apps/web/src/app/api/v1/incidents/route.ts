// GET /api/v1/incidents mock — {"items":[]} 봉투·status/category 필터·created_at desc 정렬을 재현합니다.

import type { NextRequest } from 'next/server';

import {
  INCIDENT_CATEGORIES,
  INCIDENT_STATUSES,
  type IncidentCategory,
  type IncidentListItem,
  type IncidentResponse,
  type IncidentStatus,
  type IncidentsResponse,
} from '@/types/api';

import {
  errorEnvelope,
  incidentView,
  incidents,
  mockIncidentOverrides,
} from '../_mock/data';

function toListItem(i: IncidentResponse): IncidentListItem {
  return {
    incident_id: i.incident_id,
    title: i.title,
    subject_arn: i.subject_arn,
    category: i.category,
    status: i.status,
    initial_risk_level: i.initial_risk_level,
    reviewed_risk_level: i.reviewed_risk_level,
    response_mode: i.response_mode,
    created_at: i.created_at,
    updated_at: i.updated_at,
  };
}

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const status = searchParams.get('status');
  const category = searchParams.get('category');

  // 미등록 enum 값은 422 오류 봉투 (실 BE의 쿼리 검증과 동일)
  if (status !== null && !(INCIDENT_STATUSES as readonly string[]).includes(status)) {
    return errorEnvelope(
      422,
      'REQUEST_VALIDATION_FAILED',
      `status는 ${INCIDENT_STATUSES.join(' | ')} 중 하나여야 합니다`,
    );
  }
  if (category !== null && !(INCIDENT_CATEGORIES as readonly string[]).includes(category)) {
    return errorEnvelope(
      422,
      'REQUEST_VALIDATION_FAILED',
      `category는 ${INCIDENT_CATEGORIES.join(' | ')} 중 하나여야 합니다`,
    );
  }

  // mock 전용 오버라이드는 계약 필터보다 먼저 적용된다(?mock_status=FAILED&status=RESOLVED → 빈 목록)
  const overrides = mockIncidentOverrides(searchParams);

  const items = incidents
    .map((i) => incidentView(i, overrides))
    .filter((i) => status === null || i.status === (status as IncidentStatus))
    .filter((i) => category === null || i.category === (category as IncidentCategory))
    .map(toListItem)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));

  const body: IncidentsResponse = { items };
  return Response.json(body);
}
