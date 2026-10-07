#requires -RunAsAdministrator
<#
.SYNOPSIS
  在这台 Windows 上安装并初始化 Docker（Docker Desktop + WSL2 后端）。

.DESCRIPTION
  本机是 Windows 10 Enterprise LTSC 2024 (build 26200)，Hyper-V 可用但没有 WSL2 引擎，
  Docker Desktop 默认需要 WSL2。脚本做三件事：
    1. 启用 WSL2 / 虚拟机平台可选组件
    2. 安装 WSL 内核（首次可能要求重启）
    3. 下载并安装 Docker Desktop（WSL2 后端）
  只跑一次即可，重复执行会跳过已完成步骤。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File docker\install-docker.ps1
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Step($msg) { Write-Host "`n[docker] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }

# ── 0. 管理员权限 ──────────────────────────────────────────────────────────
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "`n[docker] 需要管理员权限，正在以管理员身份重启本脚本…" -ForegroundColor Yellow
    $args = '-ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
    Start-Process -FilePath 'powershell' -ArgumentList $args -Verb RunAs
    exit
}

Step '检查系统版本'
$os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Write-Host ("  {0}  build {1}.{2}" -f $os.ProductName, $os.CurrentBuild, $os.UBR)

# ── 1. 启用 WSL2 / 虚拟机平台 ──────────────────────────────────────────────
Step '启用 WSL2 与虚拟机平台'
$features = @(
  @{ N = 'Microsoft-Windows-Subsystem-Linux';        D = 'WSL'                    },
  @{ N = 'VirtualMachinePlatform';                   D = '虚拟机平台'              },
  @{ N = 'Hyper-VPlatform';                          D = 'Hyper-V 平台 (GPU 直通)' }
)
$needReboot = $false
foreach ($f in $features) {
  $state = (dism.exe /online /Get-FeatureInfo /FeatureName:$($f.N) 2>$null |
            Select-String 'State\s*:\s*(\w+)').Matches[0].Groups[1].Value
  if ($state -ne 'Enabled') {
    Write-Host "  启用 $($f.D) ($($f.N)) …"
    dism.exe /online /Enable-Feature /FeatureName:$($f.N) /All /NoRestart | Out-Null
    $needReboot = $true
  } else { Ok ("{0} 已启用" -f $f.D) }
}

if ($needReboot) {
  Write-Host "`n[docker] 系统组件已改，需要重启才能继续。" -ForegroundColor Yellow
  Write-Host "  重启后请再执行一次本脚本，接着会装 WSL 内核 + Docker Desktop。" -ForegroundColor Yellow
  Read-Host "  按回车立即重启（或直接关闭，之后手动重启）"
  shutdown /r /t 5 /c "Docker 安装：重启以启用 WSL2"
  exit
}

# ── 2. 安装 WSL 内核 ───────────────────────────────────────────────────────
Step '安装 WSL 内核（wsl.exe --install）'
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
  wsl.exe --install --web-download | Out-Host
  if ($LASTEXITCODE -eq 0) { Ok 'WSL 已安装' } else { Write-Host "  wsl 安装返回 $LASTEXITCODE，手跑一次: wsl --install" -ForegroundColor Yellow }
} else {
  Ok 'WSL 已存在'
}

# ── 3. 安装 Docker Desktop ─────────────────────────────────────────────────
Step '下载 Docker Desktop 安装器'
$dl   = 'https://desktop.docker.com/win/main/amd64/DockerDesktopInstaller.exe'
$dest = Join-Path $env:TEMP 'DockerDesktopInstaller.exe'
if (-not (Test-Path $dest)) {
  Invoke-WebRequest -Uri $dl -OutFile $dest -UseBasicParsing
  Ok ("已下载到 {0}" -f $dest)
} else { Ok '安装器已存在，跳过下载' }

Step '安装 Docker Desktop（约 1 GB，请等待）'
$proc = Start-Process -FilePath $dest -ArgumentList 'install','--accept-license' -PassThru
$proc.WaitForExit()
Ok ("安装器退出码 {0}" -f $proc.ExitCode)

Write-Host @"

[docker] 完成。接下来：
  1) 首次双击启动 Docker Desktop，接受服务协议，等它变绿
  2) Settings > General > 勾选 "Use the WSL 2 based engine"（默认已是）
  3) Settings > Resources > File sharing 里确认 <你的项目盘符> 已勾选
  4) 验证：  docker --version
             docker compose version
  5) 部署：  cd novel-to-video-codex
             docker compose up -d --build
             http://localhost:8190
"@ -ForegroundColor Green

if ($needReboot) { Write-Host "（系统可能仍需一次重启才能启动 Docker 引擎）" }
