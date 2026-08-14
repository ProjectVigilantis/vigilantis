// GET /api/v1/assets mock — 자산 봉투를 반환합니다(계약상 쿼리 파라미터 없음, mock 전용 오버라이드만 받음).

import type { NextRequest } from 'next/server';

import { COLLECTION_STATUSES, type AssetsResponse, type CollectionStatus } from '@/types/api';

import { assetsResponse } from '../_mock/data';

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const raw = params.get('mock_collection_status');
  const status = COLLECTION_STATUSES.includes(raw as CollectionStatus)
    ? (raw as CollectionStatus)
    : assetsResponse.collection_status;
  // NOT_COLLECTED는 last_collected_at·items가 비어 있어야 한다(서버 불변식)
  const empty = params.get('mock_empty') === '1' || status === 'NOT_COLLECTED';

  const body: AssetsResponse = {
    collection_status: status,
    last_collected_at: status === 'NOT_COLLECTED' ? null : assetsResponse.last_collected_at,
    items: empty ? [] : assetsResponse.items,
  };
  return Response.json(body);
}
