# install-minimax-windows.ps1 — MiniMax Code 每日签到一键安装（Windows）
#
# 仓库：https://github.com/zoujinglong666/workbuddy-auto-signin-trae
# 协议：MIT（签到接口逆向自 MiniMax Code 桌面端，仅供个人自动化使用）
#
# 用法：在本仓库目录下，用 PowerShell 运行
#     powershell -ExecutionPolicy Bypass -File .\install-minimax-windows.ps1
#
# 会创建两个定时任务：
#   1) MiniMaxAutoCheckin   每天 00:07   签到领积分，静默写 minmax_signin.log
#   2) MiniMaxCheckinRetry  每小时 :23   补签（已签则零请求）
#
# 零 Token、无窗口、关机错过后开机自动补跑。只读本机客户端登录态，
# 优先使用分发包内置的绿色版 Python，同事无需安装 Python。

$ErrorActionPreference = "Stop"

# ====== 一般不用改；自动探测失败时手动填这两个 ======
$ManualPythonw = ""   # 例如 C:\Python313\pythonw.exe
$ManualSignin  = ""   # 例如 C:\Users\You\workbuddy-auto-signin\minmax_signin.py
# ====================================================

function Test-Pythonw {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    # Windows 商店的应用执行别名（WindowsApps\pythonw.exe）不是真 Python，必须排除
    if ($Exe -like '*\WindowsApps\*') { return $false }
    $py = Join-Path (Split-Path -Parent $Exe) 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { return $false }
    try {
        $probe = (& $py -c 'import sys;sys.stdout.write(str(sys.version_info[0]))' 2>$null | Out-String).Trim()
        return (($LASTEXITCODE -eq 0) -and ($probe -eq '3'))
    } catch { return $false }
}

function Find-Pythonw {
    # 0) 分发包自带的绿色版 Python（同事无需安装）
    $scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $runtimeDir = Join-Path $scriptRoot 'runtime\python'
    $bundled = Join-Path $runtimeDir 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $bundled)) {
        $zip = Join-Path $scriptRoot 'runtime\python-3.11.9-embed-amd64.zip'
        if (Test-Path -LiteralPath $zip) {
            try {
                if (-not (Test-Path -LiteralPath $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
                Expand-Archive -LiteralPath $zip -DestinationPath $runtimeDir -Force
            } catch { }
        }
    }
    if (Test-Pythonw $bundled) { return $bundled }

    # 1) PATH 里就有 pythonw（跳过 Windows 商店别名）
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (Test-Pythonw $c) { return $c }

    # 2) PATH 里有 python.exe，取同目录的 pythonw
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
        if (Test-Pythonw $c) { return $c }
    }

    # 3) 常见安装目录（很多 Windows 装机 Python 并不进 PATH，这一步最常命中）
    foreach ($pat in @(
        'C:\Python3*\pythonw.exe',
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        'C:\Program Files\Python3*\pythonw.exe'
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit -and (Test-Pythonw $hit.FullName)) { return $hit.FullName }
    }

    # 4) py 启动器（装在 C:\Windows，几乎总在）
    $launcher = Join-Path $env:SystemRoot 'py.exe'
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c 'import sys;sys.stdout.write(sys.executable)' 2>$null | Select-Object -Last 1)
            if ($exe) {
                $c = Join-Path (Split-Path -Parent $exe.Trim()) 'pythonw.exe'
                if (Test-Pythonw $c) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "MiniMax Code 每日签到 · 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$pythonw = $ManualPythonw
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) { $pythonw = Find-Pythonw }
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到可用的 pythonw.exe。PATH 里的 python 可能是 Windows 商店别名（WindowsApps\python.exe），它不是真 Python。" -ForegroundColor Yellow
    Write-Host "请到 https://www.python.org/downloads/ 安装 Python 3（勾选 Add Python to PATH），装完重跑本脚本。" -ForegroundColor Yellow
    Write-Host "或把脚本顶部的 `$ManualPythonw 填成真实 pythonw.exe 的完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

# --- 2. 定位 minmax_signin.py ---
Write-Host "[2/3] 定位 minmax_signin.py ..." -NoNewline
$signin = $ManualSignin
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    if ($PSScriptRoot) { $signin = Join-Path $PSScriptRoot "minmax_signin.py" }
}
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) { $signin = Join-Path (Get-Location) "minmax_signin.py" }
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 minmax_signin.py。请把本脚本和 minmax_signin.py 放在同一目录后重跑。" -ForegroundColor Yellow
    exit 1
}
$signin = (Resolve-Path -LiteralPath $signin).Path
Write-Host " $signin" -ForegroundColor Green

# --- 3. 创建两个定时任务 ---
Write-Host "[3/3] 创建定时任务 ..." -NoNewline
$taskRows = @(
    @{ Name = "MiniMaxAutoCheckin";  When = "每天 00:00~08:00 随机";  What = "签到领积分" },
    @{ Name = "MiniMaxCheckinRetry"; When = "每小时 :23"; What = "补签（已签则零请求）" }
)
$registeredTasks = @()
$verifiedTasks = @()
$operation = "创建"
$currentTask = "MiniMaxAutoCheckin"
try {
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if (-not $userId) { $userId = "$env:USERDOMAIN\$env:USERNAME" }
    if (-not $userId) { $userId = $env:USERNAME }
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive `
        -RunLevel Limited -ErrorAction Stop

    # 任务 1：每日签到。基础触发 00:00 + 8 小时随机延迟，每天 00:00~08:00
    # 之间随机一个时刻运行，打散固定时间规律；每小时补签任务兜底。
    $act1 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent" -ErrorAction Stop
    $tri1 = New-ScheduledTaskTrigger -Daily -At "00:00" -ErrorAction Stop
    $tri1.RandomDelay = "PT8H"
    $set1 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act1 -Trigger $tri1 -Settings $set1 -Principal $principal `
        -Description "MiniMax Code daily check-in (silent, zero token)" -Force -ErrorAction Stop | Out-Null
    $registeredTasks += $currentTask

    # 任务 2：每小时补签，24 个独立 Daily 触发器（错过后开机可补跑）
    $currentTask = "MiniMaxCheckinRetry"
    $tri2 = @()
    for ($h = 0; $h -lt 24; $h++) {
        $tri2 += New-ScheduledTaskTrigger -Daily -At ("{0:00}:23" -f $h) -ErrorAction Stop
    }
    $act2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent-poll" -ErrorAction Stop
    $set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -ErrorAction Stop
    Register-ScheduledTask -TaskName $currentTask -TaskPath "\" `
        -Action $act2 -Trigger $tri2 -Settings $set2 -Principal $principal `
        -Description "MiniMax Code check-in catch-up poll (skips when already signed)" -Force -ErrorAction Stop | Out-Null
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
    Write-Host ("  {0,-20} {1,-7} {2,-12} {3}" -f $row.Name, $row.State, $row.When, $row.What)
    Write-Host ("  {0,-20} 下次运行 {1}" -f "", $row.NextRunTime) -ForegroundColor DarkGray
}

Write-Host ""
$logFile = Join-Path (Split-Path -LiteralPath $signin) "minmax_signin.log"
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "任务只在真领到或出错时写日志；已签到不落盘。" -ForegroundColor DarkGray
Write-Host ""
Write-Host "卸载（两个一起删）：" -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "MiniMaxAutoCheckin" -Confirm:$false' -ForegroundColor DarkGray
Write-Host '  Unregister-ScheduledTask -TaskName "MiniMaxCheckinRetry" -Confirm:$false' -ForegroundColor DarkGray
Write-Host ""
