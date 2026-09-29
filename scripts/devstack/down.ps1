<#
.SYNOPSIS
  up.ps1로 띄운 개발 스택을 내린다. 사용법: scripts/devstack/README.md

.DESCRIPTION
  FE 창을 닫고 api · localstack · db 컨테이너를 멈춘다(stop). 컨테이너와 DB 볼륨은 지우지
  않는다 — 다음 up.ps1이 그대로 이어 쓴다. LocalStack 상태는 재시작하면 비워지며 up.ps1이 다시 시드한다.
#>
[CmdletBinding()]
param(
    # FE · api만 내리고 db · localstack은 남긴다(pytest 등에 계속 쓸 때)
    [switch]$KeepInfra,
    # up.ps1에 준 FE 포트 — PID 파일이 없을 때 이 포트를 연 프로세스를 멈춘다
    [int]$WebPort = 3000
)

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$md5 = [Security.Cryptography.MD5]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLower()))
$PidFile = Join-Path $env:TEMP ('vigilantis-devstack-web-{0}.pid' -f (-join ($md5[0..3] | ForEach-Object { $_.ToString('x2') })))

function Step($m) { Write-Host "▶ $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "⚠ $m" -ForegroundColor Yellow }

# --- FE -------------------------------------------------------------------------
$stopped = $false
if (Test-Path $PidFile) {
    $webPid = [int](Get-Content -LiteralPath $PidFile -TotalCount 1)
    if (Get-Process -Id $webPid -ErrorAction SilentlyContinue) {
        Step "FE 창 닫기(PID $webPid · 자식 node 포함)"
        # /T — 창 아래의 npm · next dev(node) 트리까지 함께 끝낸다
        taskkill /PID $webPid /T /F | Out-Null
        $stopped = $true
    }
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
}
if (-not $stopped) {
    # up.ps1 밖에서 띄운 FE — 포트를 연 프로세스가 node일 때만 멈춘다
    $owner = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    $proc = if ($owner) { Get-Process -Id $owner -ErrorAction SilentlyContinue }
    if ($proc -and $proc.ProcessName -eq 'node') {
        Step "FE 중지(:$WebPort · node PID $owner)"
        taskkill /PID $owner /T /F | Out-Null
    } elseif ($proc) {
        Warn ":$WebPort 를 연 프로세스가 node가 아님($($proc.ProcessName)) — 건드리지 않음"
    } else {
        Write-Host "FE 없음(:$WebPort)" -ForegroundColor DarkGray
    }
}

# --- 컨테이너 --------------------------------------------------------------------
$services = if ($KeepInfra) { @('api') } else { @('api', 'localstack', 'db') }
Step "컨테이너 중지: $($services -join ' · ')"
docker compose -f (Join-Path $Root 'docker-compose.yml') stop @services
if ($LASTEXITCODE -ne 0) { Warn "docker compose stop 실패 — Docker Desktop이 켜져 있는지 확인" ; exit 1 }

Write-Host "✓ 내림 — 다시 띄우기: scripts\devstack\up.ps1" -ForegroundColor Green
