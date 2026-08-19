// API 호출 계층 — 오류 봉투를 ApiError로 변환하고 계약 엔드포인트 4종의 타입드 함수를 제공합니다.

import type {
  AssetsResponse,
  ErrorCode,
  ErrorResponse,
  ExecuteActionRequest,
  ExecuteActionResponse,
  IncidentCategory,
  IncidentResponse,
  IncidentStatus,
  IncidentsResponse,
} from '@/types/api';

/**
 * NEXT_PUBLIC_API_BASE_URL 미설정이면 자체 origin(= mock Route Handler).
 * 서버 실행 구간은 상대 경로 fetch가 불가능해 로컬 dev 서버 origin으로 대체한다.
 */
function baseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (configured) return configured.replace(/\/$/, '');
  // ponytail: 서버 측 자체 origin은 localhost 가정 — 실 BE 전환은 환경변수로만 한다.
  if (typeof window === 'undefined') return `http://127.0.0.1:${process.env.PORT ?? 3000}`;
  return '';
}

/** REST 오류 봉투({"error":{code,message,request_id}})를 담은 typed error. */
export class ApiError extends Error {
  readonly httpStatus: number;
  readonly code: ErrorCode;
  readonly requestId: string;

  constructor(httpStatus: number, code: ErrorCode, message: string, requestId: string) {
    super(message);
    this.name = 'ApiError';
    this.httpStatus = httpStatus;
    this.code = code;
    this.requestId = requestId;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl()}/api/v1${path}`, { cache: 'no-store', ...init });
  const body: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = (body as ErrorResponse | null)?.error;
    throw new ApiError(
      response.status,
      detail?.code ?? 'INTERNAL_ERROR',
      detail?.message ?? `요청이 실패했습니다 (HTTP ${response.status})`,
      detail?.request_id ?? '',
    );
  }
  return body as T;
}

export function getAssets(): Promise<AssetsResponse> {
  return request<AssetsResponse>('/assets');
}

export function getIncidents(filter?: {
  status?: IncidentStatus;
  category?: IncidentCategory;
}): Promise<IncidentsResponse> {
  const query = new URLSearchParams();
  if (filter?.status) query.set('status', filter.status);
  if (filter?.category) query.set('category', filter.category);
  const suffix = query.size > 0 ? `?${query}` : '';
  return request<IncidentsResponse>(`/incidents${suffix}`);
}

export function getIncident(incidentId: string): Promise<IncidentResponse> {
  return request<IncidentResponse>(`/incidents/${encodeURIComponent(incidentId)}`);
}

/** 202(신규 예약)·200(같은 Key 재요청) 모두 같은 본문이라 응답만 돌려준다. */
export function executeAction(body: ExecuteActionRequest): Promise<ExecuteActionResponse> {
  return request<ExecuteActionResponse>('/actions/execute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** idempotency_key는 클릭 시점에 FE가 생성한다(재사용은 같은 요청의 중복 클릭·재시도 한정). */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
