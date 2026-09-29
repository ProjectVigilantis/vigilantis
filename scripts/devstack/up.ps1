<#
.SYNOPSIS
  로컬 개발 스택(DB · LocalStack · API · FE)을 한 번에 띄운다. 사용법: scripts/devstack/README.md

.DESCRIPTION
  1. Docker 데몬 확인 — 꺼져 있으면 Docker Desktop을 켜고 기다린다
  2. db · localstack 기동(healthcheck 통과까지 대기)
  3. LocalStack 시드(scripts/seed_localstack.py) — LocalStack은 재시작하면 비워진다
  4. api 기동(migrate가 depends_on으로 먼저 돈다) → GET /health 200까지 대기
  5. FE dev 서버를 새 창으로 띄운다(apps/web, -H localhost)

  포트·스택 이름은 저장소 루트 .env(APP_PORT · LOCALSTACK_PORT · COMPOSE_PROJECT_NAME)를 따른다.
  이미 떠 있는 것은 그대로 쓴다 — 여러 번 실행해도 된다.
#>
[CmdletBinding()]
param(
    # AI 과금 경로(수집→판정 스캔 · AI 분석/실행 디스패치)를 끄고 api를 띄운다
    [switch]$NoAi,
    # FE를 띄우지 않는다(BE만)
    [switch]$NoWeb,
    # LocalStack 시드를 건너뛴다
    [switch]$NoSeed,
    # FE dev 서버 포트
    [int]$WebPort = 3000,
    # 각 대기 단계(Docker 기동 · api /health · FE 포트)의 제한 시간(초)
    [int]$TimeoutSeconds = 180
)

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Web = Join-Path $Root 'apps\web'
# FE 창 PID — down.ps1이 창째 닫는 데 쓴다. 저장소(워크트리)마다 따로 두려고 경로 해시를 붙인다
# (String.GetHashCode는 PowerShell 7에서 프로세스마다 달라 MD5를 쓴다)
$md5 = [Security.Cryptography.MD5]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLower()))
$PidFile = Join-Path $env:TEMP ('vigilantis-devstack-web-{0}.pid' -f (-join ($md5[0..3] | ForEach-Object { $_.ToString('x2') })))

function Step($m) { Write-Host "▶ $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "⚠ $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "✗ $m" -ForegroundColor Red; Pop-Location; exit 1 }

# .env 파서 — KEY=VALUE 줄만 읽고 뒤에 나온 값이 이긴다(compose와 같은 규칙)
function Read-DotEnv($path) {
    $vars = @{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $vars[$Matches[1]] = $Matches[2].Trim('"', "'")
        }
    }
    $vars
}

function Get-Env($vars, $key, $default) {
    if ($vars.ContainsKey($key) -and $vars[$key]) { $vars[$key] } else { $default }
}

function Test-Listening($port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Test-DockerDaemon {
    # stderr를 버리되 Windows PowerShell 5.1에서 NativeCommandError로 번지지 않게 Continue로 부른다
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; $LASTEXITCODE -eq 0 }
    finally { $ErrorActionPreference = $old }
}

function Wait-Until([scriptblock]$cond, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $cond) { return $true }
        Start-Sleep -Seconds 2
    }
    $false
}

Push-Location $Root

# --- 0. 설정 -------------------------------------------------------------------
$EnvPath = Join-Path $Root '.env'
if (-not (Test-Path $EnvPath)) { Fail ".env 없음 — 저장소 루트에서 .env.example 을 .env 로 복사해 값을 채운다" }
$dotenv = Read-DotEnv $EnvPath
$ApiPort = Get-Env $dotenv 'APP_PORT' '8000'
$LsPort = Get-Env $dotenv 'LOCALSTACK_PORT' '4566'
$Project = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { Get-Env $dotenv 'COMPOSE_PROJECT_NAME' 'vigilantis' }

$ComposeFiles = @('-f', (Join-Path $Root 'docker-compose.yml'))
if ($NoAi) { $ComposeFiles += @('-f', (Join-Path $PSScriptRoot 'compose.no-ai.yml')) }

Write-Host "저장소 $Root · 스택 $Project · api :$ApiPort · localstack :$LsPort" -ForegroundColor DarkGray

# --- 1. Docker ------------------------------------------------------------------
if (-not (Test-DockerDaemon)) {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $desktop)) { Fail "Docker 데몬이 꺼져 있고 Docker Desktop을 찾지 못함($desktop) — 직접 켠 뒤 다시 실행" }
    Step "Docker Desktop 기동 — 데몬 대기(최대 ${TimeoutSeconds}s)"
    Start-Process $desktop
    if (-not (Wait-Until { Test-DockerDaemon } $TimeoutSeconds)) { Fail "Docker 데몬이 ${TimeoutSeconds}s 안에 뜨지 않음" }
}

# --- 2. DB · LocalStack ---------------------------------------------------------
Step "db · localstack 기동(healthcheck 대기)"
docker compose @ComposeFiles up -d --wait db localstack
if ($LASTEXITCODE -ne 0) { Fail "db · localstack 기동 실패 — 포트(5432 · $LsPort) 점유 여부를 확인: docker ps" }

# --- 3. 시드 --------------------------------------------------------------------
if ($NoSeed) {
    Warn "시드 생략(-NoSeed) — LocalStack이 새로 떴다면 자산이 비어 있다"
} else {
    Step "LocalStack 시드(멱등 — 이미 있으면 건너뜀)"
    $oldEndpoint = $env:AWS_ENDPOINT_URL
    # AWS 설정은 .env를 읽지 않는다 — 호스트에서 도는 시드는 호스트 포트로 붙어야 한다
    $env:AWS_ENDPOINT_URL = "http://localhost:$LsPort"
    try { uv run python scripts/seed_localstack.py }
    finally { $env:AWS_ENDPOINT_URL = $oldEndpoint }
    if ($LASTEXITCODE -ne 0) { Fail "시드 실패 — 신규 클론이면 먼저 'uv sync --all-packages'" }
}

# --- 4. API ---------------------------------------------------------------------
$key = Get-Env $dotenv 'OPENAI_API_KEY' ''
if ($NoAi) {
    Step "api 기동 — AI 과금 경로 끔(SCAN_ENABLED · DISPATCH_ENABLED = false)"
} else {
    if ($key -and $key -ne 'sk-...') {
        Warn "api가 스캔 → AI 분석을 주기적으로 돌린다 — $EnvPath 의 OPENAI_API_KEY로 과금된다. 끄려면 -NoAi"
    }
    Step "api 기동(migrate 선행 · 첫 실행은 이미지 빌드로 수 분)"
}
docker compose @ComposeFiles up -d api
if ($LASTEXITCODE -ne 0) {
    docker compose @ComposeFiles logs --tail 40 migrate
    Fail "api 기동 실패 — 위 migrate 로그 확인"
}

$health = "http://localhost:$ApiPort/health"
$ok = Wait-Until {
    try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $health).StatusCode -eq 200 } catch { $false }
} $TimeoutSeconds
if (-not $ok) {
    docker compose @ComposeFiles logs --tail 40 api
    Fail "api가 ${TimeoutSeconds}s 안에 $health 응답하지 않음 — 위 api 로그 확인"
}

# --- 5. FE ----------------------------------------------------------------------
if ($NoWeb) {
    Warn "FE 생략(-NoWeb)"
} elseif (Test-Listening $WebPort) {
    Warn "포트 $WebPort 이미 사용 중 — FE가 떠 있다고 보고 건너뜀"
} else {
    if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
        Step "apps/web 의존성 설치(npm ci — 처음 한 번)"
        Push-Location $Web; npm ci; $npmExit = $LASTEXITCODE; Pop-Location
        if ($npmExit -ne 0) { Fail "npm ci 실패" }
    }
    $cors = Get-Env $dotenv 'CORS_ALLOW_ORIGINS' 'http://localhost:3000'
    if (($cors -split ',' | ForEach-Object { $_.Trim().TrimEnd('/') }) -notcontains "http://localhost:$WebPort") {
        Warn "api의 CORS_ALLOW_ORIGINS($cors)에 http://localhost:$WebPort 가 없어 화면 조회가 막힌다 — .env에 추가하고 api 재기동"
    }
    Step "FE dev 서버 — 새 창(:$WebPort)"
    # 이 스택의 api를 가리키게 고정한다 — 프로세스 환경변수가 apps/web/.env.local 보다 우선한다
    $cmd = @"
`$host.UI.RawUI.WindowTitle = 'vigilantis web :$WebPort'
`$env:NEXT_PUBLIC_API_BASE_URL = 'http://localhost:$ApiPort'
npm run dev -- -H localhost -p $WebPort
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    $shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
    $proc = Start-Process $shell -WorkingDirectory $Web -PassThru `
        -ArgumentList "-NoExit -NoProfile -EncodedCommand $encoded"
    Set-Content -LiteralPath $PidFile -Value $proc.Id
    if (-not (Wait-Until { Test-Listening $WebPort } $TimeoutSeconds)) {
        Warn "FE가 ${TimeoutSeconds}s 안에 :$WebPort 를 열지 않음 — 새 창의 출력 확인"
    }
}

Pop-Location
Write-Host ""
Write-Host "✓ 개발 스택 준비" -ForegroundColor Green
if (-not $NoWeb) { Write-Host "  FE       http://localhost:$WebPort" }
Write-Host "  API      http://localhost:$ApiPort/docs"
Write-Host "  내리기   scripts\devstack\down.ps1"
