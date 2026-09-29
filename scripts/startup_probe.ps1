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

$logDir = Join-Path $env:LOCALAPPDATA "openminis\logs"
$startupLog = Join-Path $logDir "startup.log"

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

    if (-not $ok) {
        # 失败时只报"没起来"没有意义：把应用自己的日志尾巴打出来。上一次 CI 就是
        # 这样才发现真相（进程健康之后立刻 NameError 崩掉）。
        $appLog = Join-Path $logDir "desktop.log"
        if (Test-Path $appLog) {
            Write-Host "--- $appLog (tail 30) ---"
            Get-Content $appLog -Tail 30 | ForEach-Object { Write-Host $_ }
        } else {
            Write-Host "no desktop.log at $appLog"
        }
    }

    if ($p -and -not $p.HasExited) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        # 给它一点时间释放端口，否则第二次启动会换一个端口
        Start-Sleep -Seconds 2
    }
}

if (Test-Path $startupLog) {
    Write-Host "--- $startupLog (last $Runs entries) ---"
    Get-Content $startupLog | Select-Object -Last $Runs | ForEach-Object { Write-Host $_ }
} else {
    Write-Host "no startup.log at $startupLog"
}

if ($healthy -lt $Runs) {
    Write-Host "::warning::the probe did not reach a healthy backend on every run"
    exit 1
}
exit 0
