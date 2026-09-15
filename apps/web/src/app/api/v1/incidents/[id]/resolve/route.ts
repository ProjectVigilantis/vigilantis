// POST /api/v1/incidents/{id}/resolve mock — 관제자 종료 처리(#199)를 재현합니다.
// Idempotency Key를 받지 않는다 — 종료는 AWS를 바꾸지 않고 상태 하나만 옮기므로
// 조건부 갱신 자체가 멱등이며, 이미 종료된 건의 재요청은 처음 저장된 판단을 그대로 돌려준다.

import type { NextRequest } from 'next/server';

import { RESOLUTION_JUDGEMENTS, type ResolutionJudgement } from '@/types/api';
import { RESOLVABLE_STATUSES } from '@/lib/incident-filter';

import {
  errorEnvelope,
  incidentView,
  incidents,
  resolutionsByIncidentId,
} from '../../../_mock/data';

/**
 * 계약 검증(extra=forbid 포함). 통과하면 판단 값, 실패하면 422 사유를 돌려준다.
 * 성공 값 자체가 문자열이라 실행 라우터의 "문자열 = 오류" 관용구를 쓸 수 없다.
 */
function validateRequest(
  body: unknown,
): { resolution: ResolutionJudgement } | { reason: string } {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return { reason: '요청 본문은 JSON 객체여야 합니다' };
  }
  const b = body as Record<string, unknown>;
  const extra = Object.keys(b).filter((k) => k !== 'resolution');
  if (extra.length > 0) {
    return { reason: `계약에 없는 필드는 보낼 수 없습니다: ${extra.join(', ')}` };
  }
  const { resolution } = b;
  if (
    typeof resolution !== 'string' ||
    !(RESOLUTION_JUDGEMENTS as readonly string[]).includes(resolution)
  ) {
    // `EXCESSIVE`는 종료 값이 아니라 복구 실행 트리거라 이 API에 도달하지 않는다(§4.6).
    return { reason: `resolution은 ${RESOLUTION_JUDGEMENTS.join('·')} 중 하나여야 합니다` };
  }
  return { resolution: resolution as ResolutionJudgement };
}

/** 계약 형식(초 단위 "Z")의 현재 시각. */
function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return errorEnvelope(422, 'REQUEST_VALIDATION_FAILED', 'JSON 본문을 파싱하지 못했습니다');
  }

  const parsed = validateRequest(raw);
  if ('reason' in parsed) {
    return errorEnvelope(422, 'REQUEST_VALIDATION_FAILED', parsed.reason);
  }

  const incident = incidents.find((i) => i.incident_id === id);
  if (!incident) {
    return errorEnvelope(404, 'INCIDENT_NOT_FOUND', `인시던트를 찾을 수 없습니다: ${id}`);
  }

  // 저장된 시드가 아니라 **지금 계산된 상태**로 판정한다 — 실행 mock이 상태를 옮겨 놓기 때문이다.
  const view = incidentView(incident);
  if (view.status === 'RESOLVED') {
    // 재요청은 거절이 아니라 멱등 응답이다. 처음 저장된 판단을 그대로 돌려준다.
    return Response.json(view);
  }
  if (!(RESOLVABLE_STATUSES as readonly string[]).includes(view.status)) {
    return errorEnvelope(
      409,
      'INCIDENT_NOT_RESOLVABLE',
      `종료할 수 없는 상태입니다: ${view.status}`,
    );
  }

  resolutionsByIncidentId.set(id, { resolution: parsed.resolution, resolved_at: nowIso() });
  return Response.json(incidentView(incident));
}
