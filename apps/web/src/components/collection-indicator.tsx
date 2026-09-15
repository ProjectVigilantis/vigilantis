'use client';

// GNB 우측 `수집 대상` 인디케이터 — 화면설계서 §3.1이 비워둔 두 번째 인디케이터 자리다.
// 목업 안에 있던 `실시간 연동` 바를 GNB로 올린 것이며, 값은 `GET /assets` 봉투에서 온다.
//
// ⚠️ 표시 이름이 **계정 ID**다 — 봉투가 `extra="forbid"`라 자산(회사) 이름을 담을 필드가 없다.
// `권한 없음`도 계약에 없어 `COLLECTING`·`PARTIAL`·`FAILED`를 주황 하나로 묶었다(§9.2 G4에 요청).

import { useEffect, useState } from 'react';

import { getAssets } from '@/lib/api/client';
import type { AssetsResponse, CollectionStatus } from '@/types/api';
import { cn } from '@/lib/utils';

/** `collection_status` 5종 → 3색. 무색은 속을 비워 "꺼진 상태"와 구분한다. */
const TONE: Record<CollectionStatus, { label: string; text: string; dot: string }> = {
  READY: { label: '연결됨', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  COLLECTING: { label: '수집 중', text: 'text-amber-400', dot: 'bg-amber-400' },
  PARTIAL: { label: '일부만 수집됨', text: 'text-amber-400', dot: 'bg-amber-400' },
  FAILED: { label: '수집 실패', text: 'text-amber-400', dot: 'bg-amber-400' },
  NOT_COLLECTED: {
    label: '수집 대상 없음',
    text: 'text-muted-foreground',
    dot: 'bg-transparent ring-1 ring-muted-foreground',
  },
};

/** 자산이 0건이면 계정을 알 수 없다 — 이름 자리를 상태 문구로 채우면 같은 말이 두 번 나온다. */
function accountLabel(items: AssetsResponse['items']): string | null {
  const accounts = [...new Set(items.map((a) => `${a.account_id} · ${a.region}`))];
  if (accounts.length === 0) return null;
  return accounts.length === 1 ? accounts[0] : `${accounts[0]} 외 ${accounts.length - 1}`;
}

export function CollectionIndicator() {
  const [env, setEnv] = useState<AssetsResponse | null>(null);
  const [failed, setFailed] = useState(false);

  // 클라이언트에서만 부른다 — 서버에서 그리면 수집 상태가 하이드레이션 시점과 어긋난다.
  useEffect(() => {
    let alive = true;
    getAssets()
      .then((res) => alive && setEnv(res))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, []);

  // 조회 실패는 `수집 실패`(서버가 답한 상태)와 다르다 — 서버에 못 물어본 것이다.
  if (failed) {
    return (
      <span className="flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1 text-xs whitespace-nowrap text-muted-foreground">
        <span aria-hidden className="size-1.5 rounded-full bg-transparent ring-1 ring-muted-foreground" />
        수집 상태 확인 불가
      </span>
    );
  }

  // 로딩 중에는 자리만 잡는다. w-64 = 실측 257px에 맞춘 값 — 좁으면 값이 들어올 때 GNB가 흔들린다.
  if (!env) return <span className="h-6 w-64 shrink-0" aria-hidden />;

  const tone = TONE[env.collection_status];
  const name = accountLabel(env.items);

  return (
    <span className="flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1 text-xs whitespace-nowrap">
      <span aria-hidden className={cn('size-1.5 rounded-full', tone.dot)} />
      {name ? (
        <>
          <span>{name}</span>
          <span className={cn('border-l pl-2', tone.text)}>{tone.label}</span>
        </>
      ) : (
        <span className={tone.text}>{tone.label}</span>
      )}
    </span>
  );
}
