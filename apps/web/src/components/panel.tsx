// 제목·설명이 붙은 카드 한 장 — 대시보드(DSH-001)와 자산 관제(AST-001)가 공유한다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다(metric-strip.tsx와 같은 이유) — 서버 컴포넌트인
// 대시보드와 클라이언트 컴포넌트인 자산 화면이 같은 파일을 그대로 쓴다.

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

export function Panel({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="border-b">
        <CardTitle className="text-sm">{title}</CardTitle>
        {description ? <CardDescription className="text-xs">{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}
