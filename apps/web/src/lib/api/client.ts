// API 호출 계층 — 오류 봉투를 ApiError로 변환하고 계약 엔드포인트의 타입드 함수를 제공합니다.

import type {
  AssetCostsResponse,
  AssetsResponse,
  AuditEventsResponse,
  CurrentUserResponse,
  CustomerSubscriptionResponse,
  ErrorCode,
  ErrorResponse,
  ExecuteActionRequest,
  ExecuteActionResponse,
  GroupCreateRequest,
  GroupItem,
  GroupsResponse,
  IncidentCategory,
  IncidentResponse,
  IncidentStatus,
  IncidentsResponse,
  InquiriesResponse,
  InquiryCreate,
  InquiryEventItem,
  InquiryEventsResponse,
  InquiryItem,
  InquiryStatus,
  LoginStepResponse,
  MetricsTimeseriesResponse,
  NotificationsResponse,
  PaginationParams,
  ResolutionJudgement,
  SignupRequestAccepted,
  SignupRequestCreate,
  SignupRequestItem,
  SignupRequestsResponse,
  SignupRequestStatus,
  SignupRequestStatusChange,
  StaffGroupsRequest,
  StaffGroupsResponse,
  StaffInviteRequest,
  StaffItem,
  StaffResponse,
  SubscriptionPurchasePlan,
  TenantCreateRequest,
  TenantCreateResponse,
  TenantDetail,
  TenantGroupRequest,
  TenantOnboardingResponse,
  TenantSearchResponse,
  TenantSettings,
  TenantSettingsUpdateRequest,
  TenantsResponse,
  TenantUpdateRequest,
  TenantVerifyResponse,
  TotpSetupResponse,
  UserEventsResponse,
  UserInviteRequest,
  UserListItem,
  UserRole,
  UsersResponse,
  UserStatus,
} from '@/types/api';

// `node --test`가 별칭을 못 풀어 상대 경로를 쓴다(realtime-events와 같은 이유).
import { navigateToLogin } from '../auth-redirect.ts';
import type { PlatformStatusResponse } from '../platform-status.ts';
import type { ThreatEventsResponse, ThreatRangeDays } from '../threat-events.ts';

/**
 * 요청을 실어 보내는 쪽마다 다른 두 가지 — 덧붙일 헤더와 401 처리(#492 PR 2).
 *
 * - 브라우저(기본): 쿠키는 브라우저가 싣는다. 401이면 로그인 화면으로 보낸다(`navigateToLogin`).
 * - 서버 컴포넌트(`lib/api/server.ts`): Node fetch에는 브라우저 쿠키가 없어 세션 쿠키 **하나만** 헤더로 싣고,
 *   401이면 `redirect('/login?next=…')`한다.
 *
 * 경로 · 응답 타입은 이 파일 하나에만 둔다 — 전송만 바꿔 끼운다(`createReadApi`).
 */
export interface ApiTransport {
  headers?: Record<string, string>;
  /** 401을 받았을 때 오류를 던지기 전에 부른다. 반환하면 원래 `ApiError`가 그대로 올라간다. */
  onAuthRequired?: () => void;
}

const BROWSER: ApiTransport = { onAuthRequired: navigateToLogin };
/**
 * 로그인 단계 · 로그아웃. 여기서 오는 401은 세션 만료가 아니라 단계 결과다(`AUTH_FAILED`·`AUTH_CODE_INVALID` 등) —
 * 로그인 화면이 문구로 처리하고(lib/auth-errors.ts), 로그아웃의 401은 이미 로그아웃된 것이다(user-menu.tsx).
 */
const AUTH_FLOW: ApiTransport = {};

/**
 * 계약 엔드포인트의 오리진 — **언제나 실 백엔드(core-api)를 가리킨다.**
 *
 * 자체 origin으로 떨어지는 경로를 남기지 않는다. mock Route Handler(`src/app/api/v1/**`)를 걷어낸
 * 뒤에도 그 fallback이 남아 있으면 미설정 환경에서 Next 서버가 **자기 자신에게 계약 요청을 보내고**,
 * 돌아온 404를 화면이 계약 오류로 그린다 — 원인이 설정 누락인데 화면은 백엔드 장애라고 말한다.
 * 그래서 미설정이면 compose의 core-api 기본 주소(루트 `.env`의 `APP_PORT=8000`)로 간다.
 */
export const DEFAULT_API_BASE_URL = 'http://localhost:8000';

/**
 * REST와 WebSocket이 **같은 오리진**을 쓴다(§4.8). 소켓 쪽이 raw env를 따로 읽으면 기본값이
 * 한쪽에만 걸려 REST는 붙는데 인디케이터만 `실시간 미연동`으로 남는다 — 그래서 여기 하나로 모은다.
 */
export function apiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL || DEFAULT_API_BASE_URL).replace(/\/$/, '');
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

/**
 * 응답이 오기 전에 끊긴 실패(연결 거부·DNS 등) — 오류 봉투가 없으므로 원인 코드만 문구에 남긴다.
 *
 * `fetch`가 던진 오류를 그대로 올리지 않고 **여기서 새로 만든다.** Node의 연결 오류
 * (`TypeError: fetch failed` → cause `connect ECONNREFUSED`)는 스택이 전부 `node:` 내부 프레임이다.
 * 앱 코드 프레임이 하나도 없는 Error가 dev에서 서버 컴포넌트 prop(`<ErrorState error={…} />`)으로
 * 넘어가면 React Flight 디버그 직렬화가 스트림을 깨뜨려 화면이 끝나지 않는다
 * (`chunk.reason.enqueueModel is not a function` — BE 미기동 시 조회 화면 전부가 멈추던 원인).
 * 같은 이유로 `cause`도 붙이지 않는다 — 붙이면 원래 오류가 그대로 직렬화된다.
 */
function networkFailure(error: unknown): Error {
  const cause = error instanceof Error ? error.cause : undefined;
  const code =
    typeof cause === 'object' && cause !== null && 'code' in cause && typeof cause.code === 'string'
      ? cause.code
      : null;
  return new Error(`요청이 실패했습니다 (네트워크 오류${code === null ? '' : `: ${code}`})`);
}

async function requestWithStatus<T>(
  path: string,
  init: RequestInit = {},
  transport: ApiTransport = BROWSER,
): Promise<{ httpStatus: number; body: T }> {
  let response: Response;
  try {
    // `credentials: 'include'` — 세션은 core-api가 심는 HttpOnly 쿠키다(#492). 웹(:3000)과 API(:8000)는
    // 오리진이 달라 기본값(same-origin)으로는 쿠키가 실리지 않는다. 서버(Node) 쪽 조회에는 영향이 없다 —
    // 서버는 `transport.headers`로 세션 쿠키를 직접 싣는다(lib/api/server.ts).
    response = await fetch(`${apiBaseUrl()}/api/v1${path}`, {
      cache: 'no-store',
      credentials: 'include',
      ...init,
      // 이 파일의 호출부는 헤더를 전부 객체 리터럴로 넘긴다.
      headers: { ...transport.headers, ...(init.headers as Record<string, string> | undefined) },
    });
  } catch (error) {
    throw networkFailure(error);
  }
  const body: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = (body as ErrorResponse | null)?.error;
    const error = new ApiError(
      response.status,
      detail?.code ?? 'INTERNAL_ERROR',
      detail?.message ?? `요청이 실패했습니다 (HTTP ${response.status})`,
      detail?.request_id ?? '',
    );
    // 401 = 세션 없음 · 만료 · 회수(`AUTH_REQUIRED`). 처리는 전송이 정한다 — 브라우저는 로그인 이동, 서버는 redirect.
    if (response.status === 401) transport.onAuthRequired?.();
    throw error;
  }
  return { httpStatus: response.status, body: body as T };
}

async function request<T>(path: string, init?: RequestInit, transport?: ApiTransport): Promise<T> {
  return (await requestWithStatus<T>(path, init, transport)).body;
}

function paginationSuffix(pagination?: PaginationParams): string {
  const query = new URLSearchParams();
  if (pagination?.page !== undefined) query.set('page', String(pagination.page));
  if (pagination?.page_size !== undefined) query.set('page_size', String(pagination.page_size));
  return query.size > 0 ? `?${query}` : '';
}

/**
 * 서버 컴포넌트도 부르는 조회(현재 계정 · 고객 관리 · 사용자 관리 · 자기 구독 포함). 전송을 주입받아 같은 경로 · 타입을 브라우저와 서버가 함께 쓴다 —
 * 서버 쪽은 `serverApi()`(lib/api/server.ts), 브라우저 쪽은 아래 모듈 export다.
 */
export function createReadApi(transport: ApiTransport) {
  return {
    /** 업무 조회 전에 계정 종류를 확인한다. 401 처리는 주입된 전송(SSR redirect 등)이 맡는다. */
    getMe(): Promise<CurrentUserResponse> {
      return request<CurrentUserResponse>('/auth/me', undefined, transport);
    },

    getAssets(): Promise<AssetsResponse> {
      return request<AssetsResponse>('/assets', undefined, transport);
    },

    /**
     * 대시보드 시계열 6축(DSH). `hours`는 조회 창이며 서버가 1–336으로 막는다 —
     * 계약 밖 값은 422다. 기본 72는 시드가 넣는 CPU 관측 구간과 같다.
     */
    getMetricsTimeseries(hours?: number): Promise<MetricsTimeseriesResponse> {
      const suffix = hours === undefined ? '' : `?hours=${hours}`;
      return request<MetricsTimeseriesResponse>(`/metrics/timeseries${suffix}`, undefined, transport);
    },

    getIncidents(filter?: {
      status?: IncidentStatus;
      category?: IncidentCategory;
    }): Promise<IncidentsResponse> {
      const query = new URLSearchParams();
      if (filter?.status) query.set('status', filter.status);
      if (filter?.category) query.set('category', filter.category);
      const suffix = query.size > 0 ? `?${query}` : '';
      return request<IncidentsResponse>(`/incidents${suffix}`, undefined, transport);
    },

    getIncident(incidentId: string): Promise<IncidentResponse> {
      return request<IncidentResponse>(`/incidents/${encodeURIComponent(incidentId)}`, undefined, transport);
    },

    /** 고객(테넌트) 목록 · 상세(Post-MVP T-4). 서버 컴포넌트가 세션 쿠키를 실어 부른다(app/tenants). */
    getTenants(): Promise<TenantsResponse> {
      return request<TenantsResponse>('/tenants', undefined, transport);
    },

    getTenant(tenantId: string): Promise<TenantDetail> {
      return request<TenantDetail>(`/tenants/${encodeURIComponent(tenantId)}`, undefined, transport);
    },

    /** 고객사 이름 검색(#572 7번). 담당 밖 고객사는 `accessible=false`이며 상세 접근 권한은 서버가 정한다. */
    searchTenants(q: string): Promise<TenantSearchResponse> {
      return request<TenantSearchResponse>(`/tenants/search?q=${encodeURIComponent(q)}`, undefined, transport);
    },

    /** 고객사 그룹(#572 2번). 중앙 관리계정은 전체, 관리자 직원계정은 자기 담당 그룹만 받는다. */
    listGroups(): Promise<GroupsResponse> {
      return request<GroupsResponse>('/groups', undefined, transport);
    },

    /**
     * 고객 설정(T-7). 고객사 계정은 자기 고객, 플랫폼 계정은 범위 안 고객만 — 범위 밖은 404 `TENANT_NOT_FOUND`.
     * Slack은 연결 여부만 오고 URL은 오지 않는다. 화면(app/settings · app/tenants/[id]/settings)이 부른다.
     */
    getTenantSettings(tenantId: string): Promise<TenantSettings> {
      return request<TenantSettings>(`/tenants/${encodeURIComponent(tenantId)}/settings`, undefined, transport);
    },

    /**
     * 중앙 관리자(전체 고객사 사용자) · 고객 담당자(배정된 고객의 사용자) · 고객사 ADMIN(자기 고객사) 전용(#492)
     * — 그 밖은 403 `AUTH_FORBIDDEN`. 범위는 서버가 정한다.
     * 플랫폼 계정은 목록에 오지 않는다. 사용자 관리 화면(app/users/page.tsx)이 부른다.
     */
    getUsers(): Promise<UsersResponse> {
      return request<UsersResponse>('/users', undefined, transport);
    },

    /** 플랫폼 상태(중앙 관리계정). FE 제안 계약이라 서버에 아직 없다 — 실서버는 404다(lib/platform-status.ts). */
    getPlatformStatus(): Promise<PlatformStatusResponse> {
      return request<PlatformStatusResponse>('/platform/status', undefined, transport);
    },

    /** 위협 이벤트 목록(고객사 대시보드 · 로그 화면). 계약은 types/api.ts `ThreatEventsResponse`(#572 10번). */
    getThreatEvents(days: ThreatRangeDays): Promise<ThreatEventsResponse> {
      return request<ThreatEventsResponse>(`/threat-events?days=${days}`, undefined, transport);
    },

    /**
     * 자산 유형별 최근 30일 자원 사용 금액(Cost Explorer, #572 11번). 고객사 계정은 `tenantId`를 생략한다(자기 고객사).
     * 플랫폼 계정은 범위 안 고객사 하나를 골라야 한다 — 없으면 422, 범위 밖이면 404다. 읽지 못해도 200 + `UNAVAILABLE`이다.
     */
    getAssetCosts(tenantId?: string): Promise<AssetCostsResponse> {
      const query = tenantId === undefined ? '' : `?tenant_id=${encodeURIComponent(tenantId)}`;
      return request<AssetCostsResponse>(`/costs/by-asset-type${query}`, undefined, transport);
    },

    /** 조치 실행 · 종료 판단 감사 기록(#544). 로그 화면(app/logs)이 부른다. 최신순이고 `next_cursor`를 그대로 돌려주면 다음 쪽이다. */
    getAuditEvents(query: { since?: string; cursor?: string; limit?: number } = {}): Promise<AuditEventsResponse> {
      const params = new URLSearchParams();
      if (query.since !== undefined) params.set('since', query.since);
      if (query.cursor !== undefined) params.set('cursor', query.cursor);
      if (query.limit !== undefined) params.set('limit', String(query.limit));
      const search = params.toString();
      return request<AuditEventsResponse>(`/audit-events${search === '' ? '' : `?${search}`}`, undefined, transport);
    },

    /** 플랫폼(관리자) 직원 목록: 중앙 관리계정만(#492 PR 5). 계정 관리 화면이 중앙 관리계정에게 그린다(app/users/page.tsx). */
    getStaff(): Promise<StaffResponse> {
      return request<StaffResponse>('/staff', undefined, transport);
    },

    /** 도입 신청 목록: 중앙 관리계정만(#552 · 페이지 조회 #565). 도입 신청 화면(app/signup-requests)이 부른다. */
    getSignupRequests(pagination?: PaginationParams): Promise<SignupRequestsResponse> {
      return request<SignupRequestsResponse>(`/signup-requests${paginationSuffix(pagination)}`, undefined, transport);
    },

    /** 고객사 문의 목록: 플랫폼 계정은 범위 안 전부, 고객사 계정은 자기 고객사 것(#552 · 페이지 조회 #565). 고객사 문의 화면(app/inquiries)이 부른다. */
    getInquiries(pagination?: PaginationParams): Promise<InquiriesResponse> {
      return request<InquiriesResponse>(`/inquiries${paginationSuffix(pagination)}`, undefined, transport);
    },

    /**
     * 자기 고객사의 구독(#528). 대상 고객사는 세션이 정한다. 실패는 그대로 올린다 —
     * 조회 실패를 Free 나 미등록으로 바꿔 그리지 않는다(미등록은 성공 응답의 `subscription: null`이다).
     */
    getMySubscription(): Promise<CustomerSubscriptionResponse> {
      return request<CustomerSubscriptionResponse>('/me/subscription', undefined, transport);
    },
  };
}

export const {
  getAssets, getMetricsTimeseries, getIncidents, getIncident, getTenants, getTenant, getTenantSettings,
  searchTenants, listGroups, getSignupRequests, getInquiries, getThreatEvents, getAuditEvents, getMySubscription,
} = createReadApi(BROWSER);

/**
 * 관제자 종료 처리(#199). **Idempotency Key를 받지 않는다** — 종료는 AWS를 바꾸지 않고
 * Incident 상태 하나만 옮기므로 조건부 갱신 자체가 멱등이다. 이미 종료된 건의 재요청도
 * 200이며 처음 저장된 판단을 그대로 돌려준다(schemas/api/incidents.py `ResolveIncidentRequest`).
 */
export function resolveIncident(
  incidentId: string,
  resolution: ResolutionJudgement,
  resolutionNote: string | null = null,
): Promise<IncidentResponse> {
  return request<IncidentResponse>(`/incidents/${encodeURIComponent(incidentId)}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resolution, resolution_note: resolutionNote }),
  });
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

/* ─────────────────────────── /tenants (Post-MVP T-4) ─────────────────────────── */
/* 고객 계정 연결 · 원격 회수 관리자 API(packages/schemas/api/tenants.py). 고객 계정에 실제로 들어가는
   호출은 verify · resume 둘뿐이다. `external_id`는 생성 응답과 onboarding 재발급에서만 받는다. */

const tenantPath = (tenantId: string, suffix = '') => `/tenants/${encodeURIComponent(tenantId)}${suffix}`;

export function createTenant(body: TenantCreateRequest): Promise<TenantCreateResponse> {
  return postJson<TenantCreateResponse>('/tenants', body);
}

export function updateTenant(tenantId: string, body: TenantUpdateRequest): Promise<TenantDetail> {
  return request<TenantDetail>(tenantPath(tenantId), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** 고객사의 그룹을 바꾼다(#572 2번, 중앙 관리계정만). null이면 그룹에서 뺀다. */
export function setTenantGroup(tenantId: string, groupId: string | null): Promise<TenantDetail> {
  const body: TenantGroupRequest = { group_id: groupId };
  return sendJson<TenantDetail>('PUT', tenantPath(tenantId, '/group'), body);
}

/** 204. 연결된 적 없는(PENDING_CONNECTION) 고객만 지운다. 밖이면 TENANT_STATE_CONFLICT. */
export async function deleteTenant(tenantId: string): Promise<void> {
  await request<null>(tenantPath(tenantId), { method: 'DELETE' });
}

/** 같은 external_id를 다시 받는다(재생성 아님). 역할을 지운 고객의 재연결에 쓴다. */
export function getTenantOnboarding(tenantId: string): Promise<TenantOnboardingResponse> {
  return request<TenantOnboardingResponse>(tenantPath(tenantId, '/onboarding'));
}

export function verifyTenant(tenantId: string): Promise<TenantVerifyResponse> {
  return postJson<TenantVerifyResponse>(tenantPath(tenantId, '/verify'));
}

export function suspendTenant(tenantId: string, reason: string): Promise<TenantDetail> {
  return postJson<TenantDetail>(tenantPath(tenantId, '/suspend'), { reason });
}

/** 연결 확인을 먼저 하고, OK일 때만 ACTIVE가 된다. 실패하면 SUSPENDED 그대로다. */
export function resumeTenant(tenantId: string): Promise<TenantVerifyResponse> {
  return postJson<TenantVerifyResponse>(tenantPath(tenantId, '/resume'));
}

/**
 * 고객 설정 부분 수정(T-7) — 200 저장된 설정. 본문은 바뀐 것만 싣는다(lib/tenant-settings.ts `settingsUpdate`).
 * Webhook URL이 실리므로 본문을 로그하지 않는다. 정지 고객은 409 `TENANT_SUSPENDED`다.
 */
export function updateTenantSettings(tenantId: string, body: TenantSettingsUpdateRequest): Promise<TenantSettings> {
  return request<TenantSettings>(tenantPath(tenantId, '/settings'), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* ───────────────────────────── /groups (#572 2번) ───────────────────────────── */
/* 생성 · 삭제는 중앙 관리계정만 쓴다. 목록 조회는 위 createReadApi에서 전송을 공유한다. */

/** 201 — 만든 그룹. 같은 이름은 409 `GROUP_NAME_CONFLICT`다. */
export function createGroup(body: GroupCreateRequest): Promise<GroupItem> {
  return postJson<GroupItem>('/groups', body);
}

/** 204. 그룹의 고객사는 미배정이 되고 직원의 해당 그룹 배정도 함께 지워진다. */
export async function deleteGroup(groupId: string): Promise<void> {
  await request<null>(`/groups/${encodeURIComponent(groupId)}`, { method: 'DELETE' });
}

/* ─────────────────────── /me/subscription (T-16 · #528) ─────────────────────── */
/* 유료 권리의 원천은 서버 안의 모의 Marketplace 계약이다 — 실제 AWS 구매 · 청구가 없다. 응답은 전부 바뀐 뒤의
   CustomerSubscriptionResponse다. tenant_id 는 싣지 않는다 — 대상은 세션의 고객사다. 쓰기는 고객사 중앙계정만 받는다(서버 403). */

/** 모의 Marketplace 구매 요청(202) — PENDING 계약만 생긴다. 활성화 · 실패는 서버 처리 주기가 정하고 그 전에는 권리가 없다. */
export function purchaseMySubscription(plan: SubscriptionPurchasePlan): Promise<CustomerSubscriptionResponse> {
  return postJson<CustomerSubscriptionResponse>('/me/subscription/purchase', { plan });
}

/** 활성 계약의 자동 갱신을 끈다(본문 없음). 즉시 해지가 아니다 — 권리는 지금 기간의 끝 시각까지 그대로다. */
export function cancelMyRenewal(): Promise<CustomerSubscriptionResponse> {
  return request<CustomerSubscriptionResponse>('/me/subscription/cancel', { method: 'POST' });
}

/** 서비스 이용을 즉시 중단한다(고객사 관리 화면의 정지와 같은 SUSPENDED). 계약은 해지하지 않는다. 재개는 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정이 한다. */
export function stopMyService(reason: string): Promise<CustomerSubscriptionResponse> {
  return postJson<CustomerSubscriptionResponse>('/me/subscription/stop', { reason });
}

/* ──────────────────────────── /auth (#492 PR 1) ──────────────────────────── */
/* 진행 · 세션 쿠키는 서버가 심고 읽는다. 여기서는 단계 결과만 받는다. 비밀번호 · 코드는 로그하지 않는다. */

function sendJson<T>(method: 'POST' | 'PUT', path: string, body?: unknown, transport?: ApiTransport): Promise<T> {
  return request<T>(
    path,
    {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    },
    transport,
  );
}

function postJson<T>(path: string, body?: unknown, transport?: ApiTransport): Promise<T> {
  return sendJson<T>('POST', path, body, transport);
}

export function login(email: string, password: string): Promise<LoginStepResponse> {
  return postJson<LoginStepResponse>('/auth/login', { email, password }, AUTH_FLOW);
}

export function submitNewPassword(newPassword: string): Promise<LoginStepResponse> {
  return postJson<LoginStepResponse>('/auth/login/new-password', { new_password: newPassword }, AUTH_FLOW);
}

/** 등록 비밀키를 받는다. 호출할 때마다 서버가 새 키를 만들 수 있어 단계 진입 시 1회만 부른다. */
export function startTotpSetup(): Promise<TotpSetupResponse> {
  return postJson<TotpSetupResponse>('/auth/login/totp-setup', undefined, AUTH_FLOW);
}

export function verifyTotpSetup(code: string): Promise<LoginStepResponse> {
  return postJson<LoginStepResponse>('/auth/login/totp-setup/verify', { code }, AUTH_FLOW);
}

export function submitTotp(code: string): Promise<LoginStepResponse> {
  return postJson<LoginStepResponse>('/auth/login/totp', { code }, AUTH_FLOW);
}

export function submitEmailVerification(code: string): Promise<LoginStepResponse> {
  return postJson<LoginStepResponse>('/auth/login/email-verification', { code }, AUTH_FLOW);
}

export async function logout(): Promise<void> {
  await postJson<null>('/auth/logout', undefined, AUTH_FLOW);
}

/**
 * 로그인 없이 보내는 도입 신청(랜딩 로그인 창 회원가입). 세션과 무관하므로 401이 와도 로그인으로 보내지 않는다.
 *
 * 서버는 접수 재시도 키를 본문이 아니라 `Idempotency-Key` 헤더(UUIDv4)로 필수로 받는다. 없으면 422다.
 * 같은 키 · 같은 값의 재전송은 처음 `request_id`를 202로 돌려주고, 같은 키 · 다른 값은 409
 * `IDEMPOTENCY_KEY_CONFLICT`다. 키를 언제 새로 만들지는 부르는 쪽(신청서)이 정한다.
 */
export function submitSignupRequest(body: SignupRequestCreate, idempotencyKey: string): Promise<SignupRequestAccepted> {
  return postJson<SignupRequestAccepted>('/signup-requests', body, {
    ...AUTH_FLOW,
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

/** 유휴 연장 — **사용자 입력이 있을 때만** 부른다(components/auth/session-provider.tsx). */
export async function recordActivity(): Promise<void> {
  await postJson<null>('/auth/activity');
}

/**
 * 비밀번호 재설정 코드 요청(#492 PR 3) — 204. 사용자 존재 여부 · 발송 대상은 응답하지 않는다.
 * 로그인 전 화면이라 401을 세션 만료로 다루지 않는다(AUTH_FLOW 전송).
 */
export async function requestPasswordReset(email: string): Promise<void> {
  await postJson<null>('/auth/password-reset', { email }, AUTH_FLOW);
}

/** 재설정 확인 — 204. 로그인 · 세션 발급은 하지 않는다(새 비밀번호로 다시 로그인한다). */
export async function confirmPasswordReset(email: string, code: string, newPassword: string): Promise<void> {
  await postJson<null>('/auth/password-reset/confirm', { email, code, new_password: newPassword }, AUTH_FLOW);
}

/* ──────────────────────────── /users (#492 PR 3) ──────────────────────────── */
/* 중앙 관리자 · 고객 담당자(배정된 고객만) · 고객사 ADMIN 전용이다.
   메뉴를 숨기는 것은 화면 편의이고 실제 차단은 서버 403 `AUTH_FORBIDDEN`,
   관리 범위 밖 대상은 404 `USER_NOT_FOUND`다. */

const userPath = (userId: string, suffix: string) => `/users/${encodeURIComponent(userId)}${suffix}`;

/**
 * 201 — 만든 사용자. `recover_existing`은 고객 범위가 있는 플랫폼 계정이 명시적으로 고른 경우에만 true다(고객사 ADMIN은 언제나 false).
 * `tenant_id`는 플랫폼 운영자가 반드시 싣고 고객사 ADMIN은 생략한다(본문은 lib/user-admin.ts `inviteRequestFor`).
 */
export function inviteUser(body: UserInviteRequest): Promise<UserListItem> {
  return postJson<UserListItem>('/users', body);
}

export async function resendInvite(userId: string): Promise<void> {
  await postJson<null>(userPath(userId, '/invite/resend'));
}

export async function changeUserRole(userId: string, role: UserRole): Promise<void> {
  await sendJson<null>('PUT', userPath(userId, '/role'), { role });
}

export async function changeUserStatus(userId: string, status: UserStatus): Promise<void> {
  await sendJson<null>('PUT', userPath(userId, '/status'), { status });
}

export async function revokeUserSessions(userId: string): Promise<void> {
  await postJson<null>(userPath(userId, '/sessions/revoke'));
}

/* ── /staff (#492 PR 5): 중앙 관리계정 전용. 자기 자신은 바꿀 수 없다(서버가 거절). ── */

const staffPath = (userId: string, suffix: string) => `/staff/${encodeURIComponent(userId)}${suffix}`;

/**
 * 관리자 직원계정 초대(#572 1번). 201 — 담당 고객사 없는 TENANT_MANAGER로 만들어진 직원.
 * 이미 Cognito에 있는 이메일은 409이며 자동으로 연결하지 않는다(기존 계정 연결은 운영 CLI).
 */
export function inviteStaff(body: StaffInviteRequest): Promise<StaffItem> {
  return postJson<StaffItem>('/staff', body);
}

/**
 * 초대 메일 재발송(204). 대상이 없거나 관리자 직원계정이 아니면 404,
 * Cognito에 사용자가 없거나 issuer · sub가 다르거나 상태가 FORCE_CHANGE_PASSWORD(첫 로그인 전)가 아니면 409다.
 */
export async function resendStaffInvite(userId: string): Promise<void> {
  await postJson<null>(staffPath(userId, '/invite/resend'));
}

export async function changeStaffStatus(userId: string, status: UserStatus): Promise<void> {
  await sendJson<null>('PUT', staffPath(userId, '/status'), { status });
}

/** 담당 그룹 조회 · 전체 교체(#572 2번). 대상이 관리자 직원계정(TENANT_MANAGER)이 아니거나 바뀐 것이 없으면 409다. */
export function getStaffGroups(userId: string): Promise<StaffGroupsResponse> {
  return request<StaffGroupsResponse>(staffPath(userId, '/groups'));
}

export async function replaceStaffGroups(userId: string, groupIds: string[]): Promise<void> {
  const body: StaffGroupsRequest = { group_ids: groupIds };
  await sendJson<null>('PUT', staffPath(userId, '/groups'), body);
}

export function getStaffEvents(userId: string, limit = 50): Promise<UserEventsResponse> {
  return request<UserEventsResponse>(staffPath(userId, `/events?limit=${limit}`));
}

/* ── 도입 신청 · 고객사 문의 · 관리자 알림(#552, types/api.ts 끝). ── */

/** 신청 상태를 옮긴다. 반려(REJECTED)면 `reason`이 필수다(서버가 빈 값을 422로 거절한다). */
export function changeSignupRequestStatus(
  requestId: string,
  status: SignupRequestStatus,
  reason: string | null = null,
): Promise<SignupRequestItem> {
  const body: SignupRequestStatusChange = { status, reason };
  return sendJson<SignupRequestItem>('PUT', `/signup-requests/${encodeURIComponent(requestId)}/status`, body);
}

/** 고객사 계정이 보내는 장애 · 버그 문의. 소속 고객사 · 작성자는 서버가 세션에서 채운다. */
export function submitInquiry(body: InquiryCreate): Promise<InquiryItem> {
  return postJson<InquiryItem>('/inquiries', body);
}

const inquiryPath = (inquiryId: string, suffix: string) => `/inquiries/${encodeURIComponent(inquiryId)}${suffix}`;

/** 상태를 옮긴다. `note`가 있으면 같은 처리 기록 한 줄에 메모로 남는다. */
export function changeInquiryStatus(inquiryId: string, status: InquiryStatus, note: string | null = null): Promise<InquiryItem> {
  return sendJson<InquiryItem>('PUT', inquiryPath(inquiryId, '/status'), { status, note });
}

/** 상태는 그대로 두고 운영자 메모만 처리 기록에 남긴다. 201, 쌓인 기록 한 줄을 돌려준다. */
export function addInquiryNote(inquiryId: string, note: string): Promise<InquiryEventItem> {
  return postJson<InquiryEventItem>(inquiryPath(inquiryId, '/notes'), { note });
}

/** 문의 한 건의 처리 기록(오래된 것부터). 플랫폼 계정만. */
export function getInquiryEvents(inquiryId: string): Promise<InquiryEventsResponse> {
  return request<InquiryEventsResponse>(inquiryPath(inquiryId, '/events'));
}

/** 관리자 알림 최근 50건과 읽지 않은 수(플랫폼 계정만). */
export function getNotifications(): Promise<NotificationsResponse> {
  return request<NotificationsResponse>('/notifications');
}

/** 알림을 읽음으로. `ids`가 null이면 전부. */
export async function markNotificationsRead(ids: string[] | null): Promise<void> {
  await postJson<null>('/notifications/read', { notification_ids: ids });
}

/** 사용자 1명의 관리 이력(최근 순으로 서버가 정한다). 상한 기본 50. */
export function getUserEvents(userId: string, limit = 50): Promise<UserEventsResponse> {
  return request<UserEventsResponse>(userPath(userId, `/events?limit=${limit}`));
}

/**
 * 현재 사용자. 미로그인(401)은 오류가 아니라 null이다. 유휴를 연장하지 않는다.
 * 브라우저 전송이라 401이면 로그인 이동도 함께 걸린다 — 보호된 화면에서 `/me`가 401이면 세션이 끝난 것이다.
 */
export async function getMe(): Promise<CurrentUserResponse | null> {
  try {
    return await createReadApi(BROWSER).getMe();
  } catch (error) {
    if (error instanceof ApiError && error.httpStatus === 401) return null;
    throw error;
  }
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
