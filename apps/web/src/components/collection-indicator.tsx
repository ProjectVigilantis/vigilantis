'use client';

// GNB 우측 `수집 대상` 인디케이터 — 화면설계서 §3.1이 비워둔 두 번째 인디케이터 자리다.
// 목업 안에 있던 `실시간 연동` 바를 GNB로 올린 것이며, 값은 `GET /assets` 봉투에서 온다.
//
// 이 자리는 **식별(관제 대상)** 과 **상태(수집)** 로 나눠 그린다. 회사·리전은 상태가 아니라서
// 상태 칩 안에 있으면 칩이 길어지고 무엇이 값인지 흐려진다. 칩의 모양은 `StatusChip` 하나가 쥔다 —
// 옆의 연동 칩과 같은 부품이라 한쪽만 달라질 수 없다.
//
// ⚠️ 표시 이름(회사)은 봉투에서 오지 않는다 — `extra="forbid"`라 담을 필드가 없어 웹 설정값(`ORG_NAME`)을 쓴다.
// `권한 없음`도 계약에 없어 `COLLECTING`·`PARTIAL`·`FAILED`를 주황 하나로 묶었다(§9.2 G4에 요청).

import { useEffect, useRef, useState } from 'react';

import { useRealtime } from '@/components/realtime-provider';
import { StatusChip, type ChipTone } from '@/components/status-chip';
import { getAssets } from '@/lib/api/client';
import { COLLECTION_STATUS_LABELS } from '@/lib/enum-labels';
import type { AssetsResponse, CollectionStatus } from '@/types/api';

/**
 * GNB는 layout 소유라 라우트 전환으로 remount되지 않는다 — 다시 부르지 않으면 새로고침 전까지
 * 수집 상태가 고정된다(PR #299 리뷰). 수집 상태를 알리는 WS 이벤트가 계약에 없어 주기로 따라간다.
 * 스캔 주기(`SCAN_INTERVAL_SECONDS`)보다 짧기만 하면 된다.
 */
const REFRESH_MS = 60_000;

/** `collection_status` 5종 → 칩 색조 3종. 정상만 초록이고, 손볼 것이 있는 셋은 주황으로 묶는다. */
const TONE: Record<CollectionStatus, ChipTone> = {
  READY: 'ok',
  COLLECTING: 'warn',
  PARTIAL: 'warn',
  FAILED: 'warn',
  NOT_COLLECTED: 'idle',
};

/**
 * 표시명은 §3.2 사전 하나에서 온다 — 화면마다 사전을 두면 같은 상태가 화면마다 다른 이름이 된다
 * (PR #299 리뷰). `READY`는 사전이 배지를 그리지 않는 값(null)이라 인디케이터 전용 문구를 둔다.
 */
function statusLabel(status: CollectionStatus): string {
  // READY는 사전이 배지를 그리지 않는 값(null)이라 인디케이터 전용 문구를 둔다. 칩이 속성(`수집`)을
  // 앞에 붙이므로 값은 그 뒤에 이어 붙였을 때 말이 되는 한 단어여야 한다 — `수집 정상`.
  return COLLECTION_STATUS_LABELS[status]?.label ?? '정상';
}

/**
 * 관제 대상 회사 이름. MVP는 단일 계정이라 회사도 하나이고, 기본값은 시연용 가상 회사다.
 * 실제 고객으로 바꿀 때는 `NEXT_PUBLIC_ORG_NAME`만 주면 된다(빌드 시 인라인).
 */
const ORG_NAME = process.env.NEXT_PUBLIC_ORG_NAME || '예시 중소기업 최고(주)';

/**
 * 회사 이름 뒤에 수집 리전을 붙인다. 자산이 0건이면 리전을 알 수 없어 회사 이름만 둔다.
 * 계정 ID는 이름 자리에서 빠지는 대신 `accounts`로 돌려 마우스를 올리면 보이게 한다 — 운영자가
 * 어느 계정을 보고 있는지 확인할 길은 남긴다.
 */
function scopeLabel(items: AssetsResponse['items']): { name: string; accounts: string | undefined } {
  const regions = [...new Set(items.map((a) => a.region))];
  const accounts = [...new Set(items.map((a) => a.account_id))];
  let name = ORG_NAME;
  if (regions.length > 0) {
    name += ` · ${regions[0]}${regions.length > 1 ? ` 외 ${regions.length - 1}` : ''}`;
  }
  return { name, accounts: accounts.length > 0 ? `AWS 계정 ${accounts.join(', ')}` : undefined };
}

export function CollectionIndicator() {
  const { connection } = useRealtime();
  const [env, setEnv] = useState<AssetsResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const loadRef = useRef<() => void>(() => {});

  // 클라이언트에서만 부른다 — 서버에서 그리면 수집 상태가 하이드레이션 시점과 어긋난다.
  useEffect(() => {
    let alive = true;
    const load = () =>
      getAssets()
        .then((res) => {
          if (!alive) return;
          setEnv(res);
          setFailed(false);
        })
        .catch(() => alive && setFailed(true));
    loadRef.current = load;
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // WS 재연결 = 서버가 돌아왔다는 신호다. 본문은 RealtimeProvider의 `router.refresh()`로 바로 복구되지만
  // 이 자리는 layout 소유라 refresh로 다시 부르지 않아, 주기를 기다리면 최대 REFRESH_MS 동안
  // 본문은 정상인데 `확인 불가`가 남는다. 실패 상태일 때만 부른다 — 첫 진입의 연결 성립에서 중복 조회하지 않게.
  useEffect(() => {
    if (failed && connection === 'open') loadRef.current();
  }, [failed, connection]);

  // 조회 실패는 `수집 실패`(서버가 답한 상태)와 다르다 — 서버에 못 물어본 것이다.
  if (failed) {
    return <StatusChip tone="idle" label="수집" value="확인 불가" title="수집 상태를 조회하지 못했습니다" />;
  }

  // 로딩 중에는 자리만 잡는다. 288px = 기본 회사명 · 리전 1개 · `일부만 수집됨`의 실측 폭 — 좁으면 값이
  // 들어올 때 GNB가 흔들린다. `NEXT_PUBLIC_ORG_NAME`을 바꾸거나 칩 여백을 손대면 폭도 다시 잰다.
  if (!env) return <span className="h-7 w-[288px] shrink-0" aria-hidden />;

  const { name, accounts } = scopeLabel(env.items);

  return (
    <span className="flex shrink-0 items-center gap-2.5 whitespace-nowrap">
      {/* 식별 — 상태가 아니라 "무엇을 보고 있는가"다. 칩보다 한 단계 조용하게 둔다. */}
      <span className="text-muted-foreground text-xs" title={accounts}>
        {name}
      </span>
      <StatusChip
        tone={TONE[env.collection_status]}
        label="수집"
        value={statusLabel(env.collection_status)}
        pulse={env.collection_status === 'COLLECTING'}
      />
    </span>
  );
}
