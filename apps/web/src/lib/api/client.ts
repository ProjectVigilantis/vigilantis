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

async function requestWithStatus<T>(
  path: string,
  init?: RequestInit,
): Promise<{ httpStatus: number; body: T }> {
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
  return { httpStatus: response.status, body: body as T };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await requestWithStatus<T>(path, init)).body;
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

/**
 * 202(신규 예약)·200(같은 Key 재요청)은 본문이 같고 **상태 코드로만** 갈린다 — 200이면 ACT-002에
 * `이미 접수된 요청입니다`를 함께 띄워야 하므로(§4.6 응답 처리) `replayed`로 구분해 돌려준다.
 */
export async function executeAction(
  body: ExecuteActionRequest,
): Promise<{ replayed: boolean; execution: ExecuteActionResponse }> {
  const { httpStatus, body: execution } = await requestWithStatus<ExecuteActionResponse>(
    '/actions/execute',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
  return { replayed: httpStatus === 200, execution };
}

/**
 * `idempotency_key`는 **ACT-001 모달이 열릴 때 1회** 만들어 모달 인스턴스 수명 동안 고정한다(§4.6).
 * 버튼 클릭 시점에 만들면 중복 클릭이 서로 다른 키가 되어 멱등성이 무력화된다.
 */
export function newIdempotencyKey(): string {
  // `randomUUID`는 **secure context 전용**이다 — https·localhost·127.0.0.1에서만 노출된다.
  // 시연을 `http://<퍼블릭 IP>:3000`으로 띄우면 `undefined`가 되고, React 19는 이벤트 핸들러에서
  // 던진 예외를 error boundary로 잡지 않아 **실행 버튼이 아무 반응 없이 죽는다**(PR #169 리뷰).
  // `apps/web`은 아직 Dockerfile·compose 서비스가 없어 HTTPS를 전제할 수 없다.
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();

  // getRandomValues는 secure context가 아니어도 항상 있다. RFC 4122 v4 비트만 맞춰 준다.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((n) => n.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
