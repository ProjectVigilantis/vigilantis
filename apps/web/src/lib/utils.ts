import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

import { NO_VALUE } from "@/lib/enum-labels"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * 타임존을 KST로 고정한다. 고정하지 않으면 SSR(서버 TZ, 보통 UTC)과 하이드레이션(브라우저 TZ)이
 * 서로 다른 문자열을 그려 불일치가 난다 — 로컬은 둘 다 KST라 재현되지 않고 배포에서만 터진다.
 * 관제 대상이 ap-northeast-2이므로 사용자 로컬이 아니라 운영 기준 시각으로 읽는 것이 맞다.
 */
export function formatKst(iso: string | null, clock?: '24h'): string {
  if (iso === null) return NO_VALUE
  const date = new Date(iso)
  if (clock !== '24h') return date.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' }) + ' KST'
  // Locale/ICU versions can use 시·분·초 even with h23; subscription timestamps need an unambiguous clock.
  const parts = Object.fromEntries(new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(({ type, value }) => [type, value]))
  return `${parts.year}. ${parts.month}. ${parts.day}. ${parts.hour}:${parts.minute}:${parts.second} KST`
}
