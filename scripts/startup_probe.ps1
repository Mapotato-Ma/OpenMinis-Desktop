# 量启动耗时：跑两次冻结后的 exe，分别报「进程起来 → 端口通 → /api/health 200」，
# 再把应用自己写的时间线（logs\startup.log）打出来。
#
#   pwsh -File scripts/startup_probe.ps1 -ExePath dist\OpenMinisDesktop.exe
#
# 为什么要跑两次：第一次是冷启动（要建数据目录、装内置技能、首次打开数据库），
# 用户下载后第一次双击遇到的就是它；第二次才是常态。两个数都要看。
#
# 为什么还要看应用自己的 startup.log：外面只能量到"总时长"，应用内部知道是
# onefile 解包还是内核 import 吃掉的。这台 CI 机器没有企业杀软，所以它给出的是
# 下界 —— 用户机器上的真实分解要靠那份 startup.log。

param(
    [string]$ExePath = "dist\OpenMinisDesktop.exe",
    [int]$Port = 8801,
    [int]$Runs = 2,
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $ExePath)) { Write-Error "no executable at $ExePath"; exit 1 }

# 日志目录**问应用要**，不猜。曾经这里写死 %LOCALAPPDATA%\openminis —— 内核在
# Windows 上其实用 %USERPROFILE%\openminis（LOCALAPPDATA 只放缓存目录），于是
# "失败时打印日志"这一步永远打印不出来，白丢了两轮 CI 的现场。
function Get-AppLogDir([int]$portToTest) {
    try {
        $health = Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$portToTest/api/health"
        if ($health.data_dir) { return (Join-Path $health.data_dir "logs") }
    } catch { }
    foreach ($candidate in @(
            (Join-Path $env:USERPROFILE "openminis\logs"),
            (Join-Path $env:LOCALAPPDATA "openminis\logs"))) {
        if (Test-Path $candidate) { return $candidate }
    }
    return (Join-Path $env:USERPROFILE "openminis\logs")
}

$logDir = $null   # 每次样本健康之后才定得下来

# 参数名刻意不叫 $p：下面 $p 是进程句柄，重名会读到错误的东西。
function Test-Port([int]$portToTest) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $null = $c.ConnectAsync('127.0.0.1', $portToTest).Wait(250)
        $open = $c.Connected
        $c.Close()
        return $open
    } catch { return $false }
}

function Wait-Health([int]$portToTest, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$portToTest/api/health"
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

Write-Host "=== startup probe: $ExePath ==="
$healthy = 0

for ($i = 1; $i -le $Runs; $i++) {
    $label = if ($i -eq 1) { "cold" } else { "warm" }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $ExePath -ArgumentList "--no-window", "--port", "$Port" -PassThru

    $portMs = -1
    $portDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $portDeadline) {
        if ($p.HasExited) { break }
        if (Test-Port $Port) { $portMs = $sw.ElapsedMilliseconds; break }
        Start-Sleep -Milliseconds 50
    }

    $ok = $false
    $healthMs = -1
    if ($portMs -ge 0) {
        if (Wait-Health $Port $TimeoutSeconds) {
            $healthMs = $sw.ElapsedMilliseconds
            $ok = $true
            $healthy++
        }
    }
    $sw.Stop()

    Write-Host ("run {0} ({1}): port={2}ms health={3}ms exited={4}" -f $i, $label, $portMs, $healthMs, $p.HasExited)

    # 应用自己写的那一行（解包 / 内核 / 后端各段累计毫秒）比外面量到的总时长有用得多。
    if (-not $logDir) { $logDir = Get-AppLogDir $Port }
    $appLog = Join-Path $logDir "desktop.log"
    if (Test-Path $appLog) {
        $timeline = Select-String -Path $appLog -Pattern '\[startup\]' | Select-Object -Last 1
        if ($timeline) { Write-Host ("  app timeline: " + $timeline.Line.Trim()) }
        if (-not $ok) {
            Write-Host "--- $appLog (tail 30) ---"
            Get-Content $appLog -Tail 30 | ForEach-Object { Write-Host $_ }
        }
    } elseif (-not $ok) {
        Write-Host "no desktop.log at $appLog"
    }

    if ($p -and -not $p.HasExited) {
        # onefile 是「父进程解包 + 子进程跑 Python」：只 Stop-Process 父进程会留下一个
        # 还在跑的子进程，下一个样本就连到它头上 —— 实测 run 2 只花 7ms，那是在量别人。
        & taskkill /PID $p.Id /T /F | Out-Null
    }
    # 端口必须真的空出来，否则下一个样本是假的
    $freeDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $freeDeadline) {
        if (-not (Test-Port $Port)) { break }
        Start-Sleep -Milliseconds 250
    }
}

Write-Host "log dir: $logDir"
$startupLog = Join-Path $logDir "startup.log"
if (Test-Path $startupLog) {
    Write-Host "--- $startupLog (last $Runs entries) ---"
    Get-Content $startupLog | Select-Object -Last $Runs | ForEach-Object { Write-Host $_ }
} else {
    # 这一条本身也是信息：说明 report() 没跑成，去 desktop.log 里找 [startup] 那行。
    Write-Host "no startup.log at $startupLog"
    Write-Host "--- $logDir ---"
    Get-ChildItem $logDir -ErrorAction SilentlyContinue | Select-Object Name, Length | Format-Table | Out-String | Write-Host
}

if ($healthy -lt $Runs) {
    Write-Host "::warning::the probe did not reach a healthy backend on every run"
    exit 1
}
exit 0
