'use client';

// 랜딩 전체에 깔리는 회전 지구. 화면에 고정돼(스크롤해도 제자리) 섹션들이 그 위로 지나간다.
// 지구는 화면 오른쪽 아래에 걸쳐 왼쪽 위 사분면만 보이게 둔다. 경선 · 위선 그물은 배경 격자와 같은 결이고,
// 대륙은 해안선 외곽선으로 긋는다(globe-data.ts). AWS 상용 리전마다 점과 퍼져 나가는 파동을 두고, 파동은
// 리전마다 시작 시점을 엇갈려 한꺼번에 깜빡이지 않게 한다. Vigilantis가 관제하는 서울 리전만 초록으로 크게 표시한다.
// 움직임은 [움직임 멈춤](motion-toggle.tsx)과 모션 줄이기 설정을 따른다.

import { useEffect, useRef } from 'react';

import { LAND, REGIONS, WATCHED_REGION } from '@/components/landing/globe-data';

const DEG = Math.PI / 180;
const TILT = 10 * DEG; // 북극을 조금 앞으로 숙여 북반구 중위도가 보이는 사분면에 오게 한다
const SIN_T = Math.sin(TILT);
const COS_T = Math.cos(TILT);
const TURN_MS = 120_000; // 지구 한 바퀴
const PING_MS = 2_400; // 리전 파동 한 번
const EDGE_STEP = 2; // 해안선 한 변을 이 각도(도) 이하로 잘게 나눠 구면을 따라 휘게 한다
const LINE = '124, 176, 255'; // dark-on-primary-soft(#7cb0ff)
const LIVE = '34, 197, 94'; // live(#22c55e)

const WATCHED = REGIONS.find((r) => r.code === WATCHED_REGION);
// 관제 리전이 보이는 사분면 왼쪽에서 출발해 오른쪽으로 흘러가게 시작 각을 잡는다(경도 중심 = -회전각).
const SPIN0 = (-(WATCHED?.lon ?? 127) - 48) * DEG;

/** 위도 · 경도의 삼각값을 미리 계산해 둔 점. 매 프레임은 회전각의 sin · cos만 곱한다. */
type Site = { cp: number; sp: number; cl: number; sl: number };

function site(lon: number, lat: number): Site {
  return { cp: Math.cos(lat * DEG), sp: Math.sin(lat * DEG), cl: Math.cos(lon * DEG), sl: Math.sin(lon * DEG) };
}

/** 회전각 s(sin · cos)만큼 돌린 뒤 기울여 단위 구 위의 화면 좌표로 바꾼다. z > 0이면 앞면이다. */
function turn(p: Site, ss: number, cs: number) {
  const sinLam = p.sl * cs + p.cl * ss;
  const cosLam = p.cl * cs - p.sl * ss;
  const x = p.cp * sinLam;
  const z0 = p.cp * cosLam;
  return { x, y: p.sp * COS_T - z0 * SIN_T, z: p.sp * SIN_T + z0 * COS_T };
}

/**
 * 대륙 외곽선. 윤곽마다 닫힌 꼭짓점 줄을 EDGE_STEP 간격으로 잘게 나눠 둔다. 처음 한 번만 만든다.
 * 날짜변경선(경도 ±180)에 걸린 변은 실제 해안이 아니므로 끊는다(빈 배열로 구간을 나눈다). 둘이다.
 *   - 경도 ±180 선을 따라 난 변(잘라 낸 자리).
 *   - +180에서 -180으로 건너뛰는 변. 경도로 그대로 보간하면 경도 0을 지나 지구를 한 바퀴 도는 위도선이
 *     해안선 색으로 그려진다(2026-10-29 추코트카 부근 위도 65-70°에서 실측).
 */
const coast: Site[][] = LAND.flatMap((poly) => {
  const runs: Site[][] = [[]];
  for (let i = 0; i < poly.length; i++) {
    const [lon0, lat0] = poly[i];
    const [lon1, lat1] = poly[(i + 1) % poly.length];
    if ((Math.abs(lon0) === 180 && Math.abs(lon1) === 180) || Math.abs(lon1 - lon0) > 180) {
      runs.push([]);
      continue;
    }
    const run = runs[runs.length - 1];
    const n = Math.max(1, Math.ceil(Math.max(Math.abs(lon1 - lon0), Math.abs(lat1 - lat0)) / EDGE_STEP));
    if (run.length === 0) run.push(site(lon0, lat0));
    for (let k = 1; k <= n; k++) run.push(site(lon0 + ((lon1 - lon0) * k) / n, lat0 + ((lat1 - lat0) * k) / n));
  }
  return runs.filter((r) => r.length > 1);
});

// 파동 시작 시점: 리전 순서에 황금비를 곱해 고르게 흩는다(이웃 리전끼리 같은 박자로 뛰지 않게).
const regionSites = REGIONS.map((r, i) => ({
  watched: r.code === WATCHED_REGION,
  site: site(r.lon, r.lat),
  offset: ((i * 0.618) % 1) * PING_MS,
}));

function draw(ctx: CanvasRenderingContext2D, w: number, h: number, elapsed: number) {
  ctx.clearRect(0, 0, w, h);
  // 오른쪽 아래에 걸친다: 중심은 화면 아래 경계 바로 밑, 가로는 오른쪽 1/5 지점. 윗머리가 화면 높이의
  // 40% 안팎까지 올라오고 왼쪽 끝이 화면 가운데 조금 왼쪽에 닿는다(PM 스케치 2026-10-29).
  const R = Math.max(240, Math.min(680, w * 0.38, h * 0.62));
  const cx = w * 0.8;
  const cy = h + R * 0.05;
  const sx = (x: number) => cx + x * R;
  const sy = (y: number) => cy - y * R;
  const spin = SPIN0 + (elapsed / TURN_MS) * 2 * Math.PI;
  const ss = Math.sin(spin);
  const cs = Math.cos(spin);

  // 대기: 왼쪽 위에서 빛을 받은 듯 안쪽을 옅게 채운다.
  const air = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.45, R * 0.1, cx, cy, R * 1.08);
  air.addColorStop(0, 'rgba(29, 91, 216, 0.22)');
  air.addColorStop(0.7, 'rgba(29, 91, 216, 0.07)');
  air.addColorStop(1, 'rgba(29, 91, 216, 0)');
  ctx.fillStyle = air;
  ctx.beginPath();
  ctx.arc(cx, cy, R * 1.08, 0, 2 * Math.PI);
  ctx.fill();

  // 경선 · 위선 15° 그물. 앞면만 옅게 그어 대륙 외곽선이 주인공이 되게 한다.
  const grid = new Path2D();
  const segment = (a: ReturnType<typeof turn>, b: ReturnType<typeof turn>) => {
    if (a.z <= 0 || b.z <= 0) return;
    grid.moveTo(sx(a.x), sy(a.y));
    grid.lineTo(sx(b.x), sy(b.y));
  };
  for (let lon = -180; lon < 180; lon += 15) {
    let prev = turn(site(lon, -90), ss, cs);
    for (let lat = -87; lat <= 90; lat += 3) {
      const next = turn(site(lon, lat), ss, cs);
      segment(prev, next);
      prev = next;
    }
  }
  for (let lat = -75; lat <= 75; lat += 15) {
    let prev = turn(site(-180, lat), ss, cs);
    for (let lon = -176; lon <= 180; lon += 4) {
      const next = turn(site(lon, lat), ss, cs);
      segment(prev, next);
      prev = next;
    }
  }
  ctx.lineWidth = 1;
  ctx.strokeStyle = `rgba(${LINE}, 0.1)`;
  ctx.stroke(grid);

  ctx.strokeStyle = `rgba(${LINE}, 0.4)`;
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, 2 * Math.PI);
  ctx.stroke();

  // 대륙 외곽선: 앞면 구간만. 가장자리로 갈수록 흐리게 하되 strokeStyle 전환을 줄이려고 밝기를 세 단계로 묶는다.
  const shores = [new Path2D(), new Path2D(), new Path2D()];
  for (const run of coast) {
    let prev = turn(run[0], ss, cs);
    for (let k = 1; k < run.length; k++) {
      const next = turn(run[k], ss, cs);
      if (prev.z > 0 && next.z > 0) {
        const path = shores[Math.min(2, Math.floor(Math.min(prev.z, next.z) * 3))];
        path.moveTo(sx(prev.x), sy(prev.y));
        path.lineTo(sx(next.x), sy(next.y));
      }
      prev = next;
    }
  }
  ctx.lineWidth = 1.2;
  ctx.lineJoin = 'round';
  shores.forEach((path, i) => {
    ctx.strokeStyle = `rgba(${LINE}, ${0.3 + i * 0.2})`;
    ctx.stroke(path);
  });

  // 리전: 앞면에 있을 때만 점과 퍼져 나가는 파동. 관제 리전은 다른 리전 위에 오도록 마지막에 그린다.
  const regions = [...regionSites].sort((a, b) => Number(a.watched) - Number(b.watched));
  for (const r of regions) {
    const q = turn(r.site, ss, cs);
    if (q.z <= 0) continue;
    const fade = Math.min(1, q.z * 2.5);
    const x = sx(q.x);
    const y = sy(q.y);
    const phase = ((elapsed + r.offset) % PING_MS) / PING_MS;
    const color = r.watched ? LIVE : LINE;
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = `rgba(${color}, ${(r.watched ? 0.65 : 0.5) * (1 - phase) * fade})`;
    ctx.beginPath();
    ctx.arc(x, y, (r.watched ? 4 : 2.5) + phase * (r.watched ? 22 : 14), 0, 2 * Math.PI);
    ctx.stroke();
    ctx.fillStyle = r.watched ? `rgba(${LIVE}, ${fade})` : `rgba(220, 234, 255, ${0.95 * fade})`;
    ctx.beginPath();
    ctx.arc(x, y, r.watched ? 4 : 2.5, 0, 2 * Math.PI);
    ctx.fill();
  }
}

export function GlobeBackdrop({ className }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
    const stopped = () => reduce.matches || document.documentElement.dataset.motion === 'paused';
    let w = 0;
    let h = 0;
    let elapsed = 0;
    let last = 0;
    let frame = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = rect.width;
      h = rect.height;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(ctx, w, h, elapsed);
    };
    // 화면에 고정된 배경이라 늘 보인다. 탭이 숨으면 rAF가 알아서 쉰다.
    const tick = (now: number) => {
      // 탭이 잠들었다 깨어나도 한 번에 크게 건너뛰지 않게 한 프레임 간격을 묶는다.
      if (!stopped()) {
        elapsed += Math.min(now - last, 100);
        draw(ctx, w, h, elapsed);
      }
      last = now;
      frame = requestAnimationFrame(tick);
    };

    const sizeObserver = new ResizeObserver(resize);
    sizeObserver.observe(canvas);
    resize();
    last = performance.now();
    frame = requestAnimationFrame(tick);

    return () => {
      sizeObserver.disconnect();
      cancelAnimationFrame(frame);
    };
  }, []);

  return <canvas ref={ref} aria-hidden className={className} />;
}
