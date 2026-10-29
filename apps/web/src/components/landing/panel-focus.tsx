'use client';

// 랜딩 섹션의 초점. 페이지는 보통 스크롤로 내려가고, 화면 가운데의 초점 띠를 차지한 섹션만 또렷하다.
// 띠에서 벗어날수록 내용이 흐려지고 옅어져 지금 읽는 섹션이 강조된다. 섹션 사이 여백은 좁게 둬(page.tsx)
// 박스들이 이어져 보이고, 초점만 옮겨 간다. 경계에서는 두 섹션이 조금씩 흐려 초점이 넘어간다.
//
// 초점 띠의 높이는 섹션마다 다르다. 어떤 섹션이든 또렷하게 머무는 스크롤 거리가 화면 높이의 HOLD 이상이 되게 잡는다.
// 띠 하나(가운데 40%)로 재면 숫자 · 보안처럼 세로가 짧은 섹션은 띠 안에 다 들어오는 순간이 아주 짧아
// 금방 흐려졌다(2026-10-29 PM 피드백).
//   - 긴 섹션(화면의 BASE + HOLD 이상): 가운데 BASE 띠를 다 덮는 동안 또렷하다.
//   - 짧은 섹션: 띠를 섹션 높이 + HOLD로 넓혀, 섹션이 그 안에 다 들어 있는 동안 또렷하다.
// 마지막 섹션은 페이지 끝이라 화면 가운데까지 올라오지 못해 띠 기준으로는 거의 또렷해지지 않는다. 그래서 마지막
// 섹션만 화면에 보이는 비율로도 재서, 섹션의 LAST_SEEN 이상이 화면에 보이면 또렷하게 둔다.
// 지구 배경은 건드리지 않는다. 대상은 page.tsx의 `[data-panel]` 섹션과 그 안의 `[data-panel-body]`(내용)다.
//
// 스크롤 · 크기 변화마다 한 프레임에 한 번 계산하고, 또렷한 상태로 돌아오면 인라인 스타일을 지운다.
// 모션 줄이기 설정이면 아무것도 하지 않는다. 섹션 안에 backdrop-filter를 두지 않는다: 내용에 blur가 걸리면 그 요소의
// 배경만 같이 흐려지지 않아 네모 테두리가 딱딱한 선으로 남는다(2026-10-29 미리보기 카드에서 실측).

import { useEffect } from 'react';

const BASE = 0.4; // 화면 높이 대비. 긴 섹션의 초점 띠 높이(화면 가운데)
const HOLD = 0.35; // 화면 높이 대비. 섹션이 또렷하게 머무는 최소 스크롤 거리
const LAST_SEEN = 0.75; // 마지막 섹션이 이 비율 이상 화면에 보이면 또렷하다
const BLUR = 3; // px. 띠를 전혀 차지하지 못한 섹션의 흐림
const DIM = 0.5; // 띠를 전혀 차지하지 못한 섹션에서 빠지는 불투명도

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

function clear(body: HTMLElement) {
  body.style.opacity = '';
  body.style.filter = '';
}

export function PanelFocus() {
  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
    const panels = [...document.querySelectorAll<HTMLElement>('[data-panel]')];
    const bodies = panels.map((p) => p.querySelector<HTMLElement>('[data-panel-body]'));
    let frame = 0;

    const update = () => {
      frame = 0;
      const vh = window.innerHeight;
      panels.forEach((panel, i) => {
        const body = bodies[i];
        if (!body) return;
        if (reduce.matches) return clear(body);
        const rect = panel.getBoundingClientRect();
        const band = Math.min(vh, rect.height >= vh * (BASE + HOLD) ? vh * BASE : rect.height + vh * HOLD);
        const top = (vh - band) / 2;
        const bottom = top + band;
        // 초점 띠와 섹션 가운데 작은 쪽을 기준으로 겹친 비율. 1이면 또렷, 0이면 가장 흐림.
        const overlap = Math.min(rect.bottom, bottom) - Math.max(rect.top, top);
        let share = clamp01(overlap / Math.min(band, rect.height));
        if (i === panels.length - 1) {
          const seen = (Math.min(rect.bottom, vh) - Math.max(rect.top, 0)) / rect.height;
          share = Math.max(share, clamp01(seen / LAST_SEEN));
        }
        const off = 1 - share;
        if (off < 0.01) return clear(body);
        body.style.opacity = String(1 - off * DIM);
        body.style.filter = `blur(${(off * BLUR).toFixed(2)}px)`;
      });
    };
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(update);
    };

    update();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    reduce.addEventListener('change', schedule);
    return () => {
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      reduce.removeEventListener('change', schedule);
      cancelAnimationFrame(frame);
      bodies.forEach((body) => body && clear(body));
    };
  }, []);

  return null;
}
