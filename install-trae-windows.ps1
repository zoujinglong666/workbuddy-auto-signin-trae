# install-trae-windows.ps1 — Trae 每日自动领取积分一键安装（Windows）
#
# 仓库：https://github.com/88lin/workbuddy-auto-signin
# 协议：MIT（签到实现 vendor 自 L0NE-6/Trae-AutoCheckin，见 trae/NOTICE.md）
#
# 用法：在本仓库目录下，用 PowerShell 运行
#     powershell -ExecutionPolicy Bypass -File .\install-trae-windows.ps1
#
# 会创建两个定时任务：
#   1) TraeAutoCheckin    每天 00:23   签到领积分，静默写 trae_signin.log
#   2) TraeCheckinRetry   每小时 :37   补签（已签则零请求）
#
# 两者都零 Token、无窗口、关机错过后开机自动补跑。只读本机 Trae 客户端登录态。

$ErrorActionPreference = "Stop"

# ====== 一般不用改；自动探测失败时手动填这两个 ======
$ManualPythonw = ""   # 例如 C:\Python313\pythonw.exe
$ManualSignin  = ""   # 例如 C:\Users\You\workbuddy-auto-signin\trae_signin.py
# ====================================================

function Find-Pythonw {
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($c -and (Test-Path -LiteralPath $c)) { return $c }

    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-Path -LiteralPath $c) { return $c }
    }

    foreach ($pat in @(
        "C:\Python3*\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }

    $launcher = Join-Path $env:SystemRoot "py.exe"
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c "import sys;print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($exe) {
                $c = Join-Path (Split-Path $exe.Trim()) "pythonw.exe"
                if (Test-Path -LiteralPath $c) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "Trae 每日自动领取积分 · 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$pythonw = $ManualPythonw
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) { $pythonw = Find-Pythonw }
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "装好后重跑本脚本；或把脚本顶部的 `$ManualPythonw 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

# --- 2. 定位 trae_signin.py ---
Write-Host "[2/3] 定位 trae_signin.py ..." -NoNewline
$signin = $ManualSignin
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    if ($PSScriptRoot) { $signin = Join-Path $PSScriptRoot "trae_signin.py" }
}
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) { $signin = Join-Path (Get-Location) "trae_signin.py" }
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 trae_signin.py。请把本脚本和 trae_signin.py 放在同一目录后重跑。" -ForegroundColor Yellow
    exit 1
}
$signin = (Resolve-Path -LiteralPath $signin).Path
Write-Host " $signin" -ForegroundColor Green

# --- 3. 创建两个定时任务 ---
Write-Host "[3/3] 创建定时任务 ..." -NoNewline
$taskRows = @(
    @{ Name = "TraeAutoCheckin";  When = "每天 00:23"; What = "签到领积分" },
    @{ Name = "TraeCheckinRetry"; When = "每小时 :37"; What = "补签（已签则零请求）" }
)
$registeredTasks = @()
$verifiedTasks = @()
$operation = "创建"
$currentTask = "TraeAutoCheckin"
try {
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not $userId) { $userId = "$env:USERDOMAIN\$env:USERNAME" }
    if (-not $userId) { $userId = $env:USERNAME }
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive `
        -RunLevel Limited -ErrorAction Stop

    # 任务 1：每日签到。00:23 是实测较稳的时间点——00:00~00:10 是全天最高峰，
    # 整点抢签最容易撞上服务端限流 9074。
    $act1 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent" -ErrorAction Stop
    $tri1 = New-ScheduledTaskTrigger -Daily -At "00:23" -ErrorAction Stop
    $set1 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act1 -Trigger $tri1 -Settings $set1 -Principal $principal `
        -Description "Trae daily credits check-in (silent, zero token)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 任务 2：每小时补签。用 24 个独立的 Daily 触发器，而不是"每小时重复"：
    # 重复实例一旦错过（关机/睡眠）就永久跳过，StartWhenAvailable 不会补跑。
    # 已签成功的账号当天会写进状态文件，后续触发只是零请求空跑。
    $currentTask = "TraeCheckinRetry"
    $tri2 = @()
    for ($h = 0; $h -lt 24; $h++) {
        $tri2 += New-ScheduledTaskTrigger -Daily -At ("{0:00}:37" -f $h) -ErrorAction Stop
    }
    $act2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent-poll" -ErrorAction Stop
    $set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act2 -Trigger $tri2 -Settings $set2 -Principal $principal `
        -Description "Trae check-in catch-up poll (skips when already signed)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    $operation = "校验"
    foreach ($row in $taskRows) {
        $currentTask = $row.Name
        $tasks = @(Get-ScheduledTask -TaskName $currentTask -TaskPath "\" -ErrorAction Stop)
        if ($tasks.Count -ne 1) { throw "未找到唯一的计划任务：$currentTask" }
        $info = Get-ScheduledTaskInfo -TaskName $currentTask -TaskPath "\" -ErrorAction Stop
        if ($null -eq $info) { throw "未读取到计划任务运行信息：$currentTask" }
        $verifiedTasks += @{
            Name = $row.Name; When = $row.When; What = $row.What
            State = $tasks[0].State; NextRunTime = $info.NextRunTime
        }
    }
} catch {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "$operation $currentTask 失败，安装未完成。" -ForegroundColor Red
    Write-Host "错误信息：$($_.Exception.Message)" -ForegroundColor Red
    if ($registeredTasks.Count -gt 0) {
        Write-Host ("已注册：" + ($registeredTasks -join "、") + "。这些任务会保留；排除问题后重跑可更新同名任务。") -ForegroundColor Yellow
    }
    Write-Host "若提示拒绝访问（0x80070005），请使用同一 Windows 账号以管理员身份打开 PowerShell 后重试。" -ForegroundColor Yellow
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 收尾汇报 ---
Write-Host ""
Write-Host "两个定时任务已就位：" -ForegroundColor Green
foreach ($row in $verifiedTasks) {
    Write-Host ("  {0,-20} {1,-7} {2,-10} {3}" -f $row.Name, $row.State, $row.When, $row.What)
    Write-Host ("  {0,-20} 下次运行 {1}" -f "", $row.NextRunTime) -ForegroundColor DarkGray
}

Write-Host ""
$logFile = Join-Path (Split-Path -LiteralPath $signin) "trae_signin.log"
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "任务只在真领到或出错时写日志；已签到不落盘。" -ForegroundColor DarkGray
Write-Host "想确认是否跑通：python `"$signin`" status" -ForegroundColor DarkGray
Write-Host ""
Write-Host "卸载（两个一起删）：" -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "TraeAutoCheckin" -Confirm:$false' -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "TraeCheckinRetry" -Confirm:$false' -ForegroundColor DarkGray
Write-Host ""
