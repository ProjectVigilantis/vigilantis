'use client';

// DSH-001 토폴로지 카드 — AST-001의 `AssetGraph`(#146)를 그대로 쓴다. 배치 계산(`buildTopology`)을
// 다시 쓰지 않아야 대시보드와 자산 화면이 같은 그래프를 그린다(PR #299 리뷰).
//
// **대시보드는 한 번에 EC2 한 대만 그린다.** 전수는 자산 화면(AST-001)이 맡는다 — 요약 자리에
// 전부 그리면 자산이 늘 때마다 이 카드가 페이지를 덮고, 정작 한 대의 경로와 보호 계층은 더
// 안 보인다. 무엇을 그릴지는 아래 `TopologyPicker`가 정하고, 기본값은 **공격 경로가 들어오는
// 첫 대**이며 그런 대가 없으면 **위험 순 1위**다.
//
// 카드는 **`그래프 │ 인스턴스 │ 경로 밖` 세 칸**이 나란히 선 구조다. 목록 두 칸이 400px쯤 가져가므로
// 그래프에는 900px 남짓이 남는데, 그래도 5열 정렬이 서도록 그래프의 접힘 기준을 768px로 내렸다
// (노드 상자에 폭 상한과 말줄임이 있어 좁은 폭에서도 이름이 새지 않는다 — `asset-graph.tsx`).
//
// 배치 계산은 **언제나 전량으로 한다**(`items` 전부를 넘긴다). 고른 한 대만 넣고 계산하면 나머지
// EC2에 달린 볼륨·보안 그룹이 갈 곳을 잃어 `트래픽 경로 밖`으로 내려가 — 없는 낭비를 만들어 낸다.
//
// **외부 공격 경로(#362)는 인시던트에서 온다.** 자산 응답에는 "어디서 들어왔나"가 없어, 목록 계약의
// `threat_context`를 `lib/threat-path`가 경로로 바꾸고 그래프가 자산 노드 위에 덧그린다. 자산 화면
// (AST-001)은 인시던트를 조회하지 않으므로 이 축은 대시보드에만 선다.

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { AssetGraph } from '@/components/assets/asset-graph';
import { TopologyPicker } from '@/components/dashboard/topology-picker';
import { buildTopology, sortRowsByRisk } from '@/lib/asset-graph';
import { rowThreats, threatPaths } from '@/lib/threat-path';
import type { AssetItem, IncidentListItem, UncollectedAssetType } from '@/types/api';

export function DashboardTopology({
  items,
  uncollected,
  incidents,
}: {
  items: AssetItem[];
  /** 조회를 못 한 유형 — 그래프가 빈 열을 "없음"이 아니라 "못 가져옴"으로 그리는 근거다. */
  uncollected: UncollectedAssetType[];
  /**
   * 인시던트 목록. 여기서 쓰는 것은 `threat_context` 하나이며 **조회 실패(null)와 0건을 가르지
   * 않는다** — 둘 다 그릴 경로가 없고, 그 구분은 대시보드 상태줄이 이미 맡고 있다.
   */
  incidents: IncidentListItem[] | null;
}) {
  const router = useRouter();
  const [picked, setPicked] = useState<string | null>(null);

  // AST-002는 Drawer라 자체 URL이 없다 — 자산 화면이 그 항목을 고른 상태로 여는 딥링크로 보낸다(§4.2).
  const open = (asset: AssetItem) => router.push(`/assets?asset=${encodeURIComponent(asset.arn)}`);

  // 목록 순서는 위험 순이다 — 아무것도 고르지 않은 채 들어온 관제자가 가장 먼저 봐야 할 대를
  // 이미 보고 있게 한다.
  const topology = buildTopology(items);
  const rows = sortRowsByRisk(topology.rows);
  const paths = threatPaths(incidents);

  // **기본 선택은 공격 경로가 들어오는 대가 먼저다.** 위험 순 1위와 대개 같지만 항상 같지는
  // 않다 — 전체 개방 SG가 달린 EC2는 스스로 `SKIP`이어도 인터넷에서 들어오는 경로가 있다.
  // 경로가 없으면 종전대로 위험 순 1위다.
  const attacked = rows.find((row) => rowThreats(row, paths).length > 0);
  const selectedArn = picked !== null && rows.some((r) => r.ec2.arn === picked)
    ? picked
    : (attacked?.ec2.arn ?? rows[0]?.ec2.arn ?? '');

  return (
    // 폭 기준은 뷰포트가 아니라 **이 카드가 실제로 받은 폭**이다 — 대시보드 왼쪽 열은 자산 화면보다
    // 좁다. 컨테이너는 자기 자신을 질의하지 못하므로 `@container`와 `@3xl:` 변형을 한 요소에 같이
    // 걸지 않는다(그러면 조건이 영영 안 맞아 세로로만 쌓인다).
    // 폭 기준은 뷰포트가 아니라 **이 카드가 실제로 받은 폭**이다 — 대시보드 왼쪽 열은 자산 화면보다
    // 좁다. 컨테이너는 자기 자신을 질의하지 못하므로 `@container`와 `@5xl:` 변형을 한 요소에 같이
    // 걸지 않는다(그러면 조건이 영영 안 맞아 세로로만 쌓인다).
    <div className="@container/topology">
      {/* **카드 높이를 못 박는다**(`max-h`가 아니라 `h`). 세 칸 중 하나가 길어져도 — 인스턴스가
          늘거나, 고른 대에 붙은 자원이 많거나, 경로 밖이 쌓이거나 — 카드는 그대로고 **안쪽이
          스크롤**한다. 상한만 두면 그 상한까지는 카드가 자라 아래 내용이 그만큼 밀린다.
          176px은 그래프 한 대치(155px)가 잘리지 않는 선이다. 글자·상자 치수는 그대로 두고 **창만**
          줄였으므로 목록은 지금도 몇 줄이 창 밖으로 넘어간다 — 그 몫이 스크롤이다.
          좁아서 세로로 쌓이는 배치에는 걸지 않는다(`@5xl/topology:`) — 그때는 칸이 나란히 서지
          않아 높이를 묶으면 세 칸이 각자 좁은 창을 갖게 된다. */}
      <div className="flex flex-col gap-4 @5xl/topology:h-44 @5xl/topology:flex-row @5xl/topology:gap-3">
        <div className="min-w-0 flex-1 @5xl/topology:overflow-y-auto">
          <AssetGraph
            items={items}
            uncollected={uncollected}
            threatPaths={paths}
            rowArns={[selectedArn]}
            // 경로 밖 목록은 그래프가 아니라 오른쪽 칸에서 그린다 — 그래프에 그릴 자리가 없는
            // 것들이라, 고르는 목록과 나란히 두는 편이 읽힌다.
            showOffPath={false}
            onSelect={open}
          />
        </div>

        {/* **EC2 유무로 묶지 않는다.** 경로 밖 자원(미연결 EBS·미사용 SG)은 EC2가 한 대도
            없어도 관제 대상이고, 오히려 그때가 전부 비용만 내는 상태다. 두 칸은 각자
            비어 있을 때만 사라진다 — 칸을 고르는 조건은 `TopologyPicker` 안에 있다. */}
        <TopologyPicker
          rows={rows}
          orphans={topology.orphans}
          selectedArn={selectedArn}
          onSelect={setPicked}
          onOpen={open}
          threatPaths={paths}
        />
      </div>
    </div>
  );
}
