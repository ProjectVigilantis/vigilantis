// GET /api/v1/incidents/{id} mock — 상세 응답과 미존재 시 404 오류 봉투를 재현합니다.

import type { NextRequest } from 'next/server';

import {
  errorEnvelope,
  incidentView,
  incidents,
  mockIncidentOverrides,
} from '../../_mock/data';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const incident = incidents.find((i) => i.incident_id === id);
  if (!incident) {
    return errorEnvelope(404, 'INCIDENT_NOT_FOUND', `인시던트를 찾을 수 없습니다: ${id}`);
  }
  return Response.json(incidentView(incident, mockIncidentOverrides(request.nextUrl.searchParams)));
}
