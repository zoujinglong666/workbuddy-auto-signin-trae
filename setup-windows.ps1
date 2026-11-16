# setup-windows.ps1 — 统一安装入口（面向拿到分发包的同事，Windows）
#
# 先做离线预检，再让你勾选要装哪些功能，最后调用各自的一键安装脚本。
# 三个功能完全独立，装哪个取决于同事本机登录过哪个客户端。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1                # 交互式：预检 + 勾选
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Yes           # 非交互：安装所有「可用」功能
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Only WorkBuddy,Trae -Yes
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -CheckOnly     # 只体检，不装任何东西
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Uninstall     # 卸载全部 5 个计划任务
#
# 前置条件：Windows + Python 3 + 已安装并登录过对应的桌面端客户端。

[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [switch]$CheckOnly,
    [switch]$Yes,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8
# signin.py / qoder_signin.py 默认按系统 ANSI(GBK) 输出，中文在 UTF-8 控制台会乱码。
# 只影响本向导启动的子进程；计划任务由任务计划程序独立启动，不继承此变量。
$env:PYTHONIOENCODING = 'utf-8'

$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

$features = @(
    [pscustomobject]@{
        Key = 'WorkBuddy'; Label = 'WorkBuddy 每日签到积分'
        Doctor = 'signin.py'; Installer = 'install-windows.ps1'
        Tasks = @('WorkBuddyAutoSignin', 'WorkBuddyGrowthPoll')
        Log = 'signin.log'; Require = '已安装并登录 WorkBuddy 桌面端'
    },
    [pscustomobject]@{
        Key = 'Qoder'; Label = 'Qoder CN 每日 100 Credits'
        Doctor = 'qoder_signin.py'; Installer = 'install-qoder-windows.ps1'
        Tasks = @('QoderDailyCredits')
        Log = 'qoder_signin.log'; Require = '已安装并登录 Qoder CN 桌面端'
    },
    [pscustomobject]@{
        Key = 'Trae'; Label = 'Trae CN 每日签到积分'
        Doctor = 'trae_signin.py'; Installer = 'install-trae-windows.ps1'
        Tasks = @('TraeAutoCheckin', 'TraeCheckinRetry')
        Log = 'trae_signin.log'; Require = '已安装并登录 Trae CN 桌面端'
    }
)

function Test-RealPython {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    # Windows 商店的应用执行别名（WindowsApps\python.exe）只是占位程序：
    # 运行任何命令都无输出（或弹商店），必须排除，否则 doctor 全部"无输出"。
    if ($Exe -like '*\WindowsApps\*') { return $false }
    try {
        $probe = (& $Exe -c 'import sys;sys.stdout.write(str(sys.version_info[0]))' 2>$null | Out-String).Trim()
        return (($LASTEXITCODE -eq 0) -and ($probe -eq '3'))
    } catch { return $false }
}

function Find-PythonExe {
    # 0) 分发包自带的绿色版 Python：同事完全不用安装，首次运行自动解压
    $runtimeDir = Join-Path $here 'runtime\python'
    $bundled = Join-Path $runtimeDir 'python.exe'
    if (-not (Test-Path -LiteralPath $bundled)) {
        $zip = Join-Path $here 'runtime\python-3.11.9-embed-amd64.zip'
        if (Test-Path -LiteralPath $zip) {
            Write-Host '  首次运行：正在解压内置绿色版 Python ...' -NoNewline
            try {
                if (-not (Test-Path -LiteralPath $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
                Expand-Archive -LiteralPath $zip -DestinationPath $runtimeDir -Force
                Write-Host ' OK' -ForegroundColor Green
            } catch {
                Write-Host ' 失败（将尝试使用系统 Python）' -ForegroundColor Yellow
            }
        }
    }
    if (Test-RealPython $bundled) { return $bundled }

    $candidates = @()

    # py 启动器最可靠：正式安装的 Python 通常会注册它，且不受 PATH 里商店别名干扰
    $launcher = Join-Path $env:SystemRoot 'py.exe'
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c 'import sys;sys.stdout.write(sys.executable)' 2>$null | Select-Object -Last 1)
            if ($exe) { $candidates += $exe.Trim() }
        } catch { }
    }

    $candidates += (Get-Command python -ErrorAction SilentlyContinue).Source
    $candidates += (Get-Command python3 -ErrorAction SilentlyContinue).Source

    foreach ($pat in @(
        'C:\Python3*\python.exe',
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        'C:\Program Files\Python3*\python.exe'
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { $candidates += $hit.FullName }
    }

    foreach ($c in $candidates) {
        if ($c -and (Test-RealPython $c)) { return $c }
    }
    return $null
}

function Get-DoctorResult {
    param([string]$PythonExe, [string]$ScriptPath)
    $output = (& $PythonExe $ScriptPath 'doctor' 2>&1 | Out-String).Trim()
    $code = $LASTEXITCODE
    if (-not $output) {
        return [pscustomobject]@{ Ok = $false; Report = "doctor 无输出（退出码 $code）；请确认已登录对应客户端" }
    }
    $json = $null
    try { $json = $output | ConvertFrom-Json } catch {
        foreach ($line in ($output -split '\r?\n')) {
            if ($line.TrimStart().StartsWith('{')) {
                try { $json = $line | ConvertFrom-Json; break } catch { }
            }
        }
    }
    if (-not $json) {
        $preview = $output.Substring(0, [Math]::Min(160, $output.Length)).Replace("`n", ' ')
        return [pscustomobject]@{ Ok = $false; Report = "doctor 输出异常（退出码 $code）：$preview" }
    }
    $bad = @('NO_AUTH', 'NO_SESSION', 'AUTH_ERROR', 'ERROR', '')
    $ok = ($json.needs_attention -ne $true) -and ($bad -notcontains [string]$json.result)
    return [pscustomobject]@{ Ok = $ok; Report = [string]$json.report }
}

function Remove-AllTasks {
    Write-Host ''
    Write-Host '卸载全部计划任务' -ForegroundColor Cyan
    foreach ($f in $features) {
        foreach ($t in $f.Tasks) {
            $existing = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            if ($existing) {
                try {
                    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
                    Write-Host ('  已删除 {0}' -f $t) -ForegroundColor Green
                } catch {
                    Write-Host ('  删除失败 {0}: {1}' -f $t, $_.Exception.Message) -ForegroundColor Red
                }
            } else {
                Write-Host ('  本就没有 {0}' -f $t) -ForegroundColor DarkGray
            }
        }
    }
    Write-Host ''
    Write-Host '注意：脚本和日志仍保留在本目录，可自行删除整个文件夹。' -ForegroundColor DarkGray
}

Write-Host ''
Write-Host '自动签到工具箱 · 安装向导' -ForegroundColor Cyan
Write-Host ('-' * 52) -ForegroundColor DarkGray
Write-Host ('目录：{0}' -f $here) -ForegroundColor DarkGray

if ($Uninstall) {
    Remove-AllTasks
    exit 0
}

# --- 1. Python 与文件完整性 ---
Write-Host ''
Write-Host '[1/3] 检查运行环境 ...' -ForegroundColor Yellow
$python = Find-PythonExe
if (-not $python) {
    Write-Host '  没找到可用的 Python 3。' -ForegroundColor Red
    $pathPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($pathPython -like '*\WindowsApps\*') {
        Write-Host ('  PATH 里的 python 是 Windows 商店别名（{0}），不是真正的 Python。' -f $pathPython) -ForegroundColor Yellow
        Write-Host '  请二选一：' -ForegroundColor Yellow
        Write-Host '   A. 到 https://www.python.org/downloads/ 安装 Python 3，勾选 "Add Python to PATH"' -ForegroundColor Yellow
        Write-Host '   B. 若已从商店装过 Python：设置 → 应用 → 高级应用设置 → 应用执行别名，关闭 python.exe 的别名' -ForegroundColor Yellow
    } else {
        Write-Host '  请安装 Python 3：https://www.python.org/downloads/ ，勾选 "Add Python to PATH"，装完重跑本向导。' -ForegroundColor Yellow
    }
    exit 1
}
$version = (& $python '--version' 2>&1 | Out-String).Trim()
Write-Host ('  Python: {0}  ({1})' -f $version, $python) -ForegroundColor Green
if ($python -like '*\runtime\python\*') {
    Write-Host '  使用分发包内置 Python，无需在系统安装' -ForegroundColor DarkGray
}

$missing = @()
foreach ($f in $features) {
    foreach ($rel in @($f.Doctor, $f.Installer)) {
        if (-not (Test-Path -LiteralPath (Join-Path $here $rel))) { $missing += $rel }
    }
}
if ($missing.Count -gt 0) {
    Write-Host ('  分发包不完整，缺少：{0}' -f ($missing -join '、')) -ForegroundColor Red
    Write-Host '  请重新解压完整的 zip（保留 trae 子目录），不要只复制单个 .py 文件。' -ForegroundColor Yellow
    exit 1
}
Write-Host '  文件完整' -ForegroundColor Green

# --- 2. 逐功能离线体检 ---
Write-Host ''
Write-Host '[2/3] 体检（只读取本机登录态，不联网、不领取）...' -ForegroundColor Yellow
$checks = @()
for ($i = 0; $i -lt $features.Count; $i++) {
    $f = $features[$i]
    $r = Get-DoctorResult -PythonExe $python -ScriptPath (Join-Path $here $f.Doctor)
    $checks += [pscustomobject]@{ Index = $i + 1; Feature = $f; Ok = $r.Ok; Report = $r.Report }
    if ($r.Ok) {
        Write-Host ('  [{0}] [可用]   {1}' -f ($i + 1), $f.Label) -ForegroundColor Green
        Write-Host ('        {0}' -f $r.Report) -ForegroundColor DarkGray
    } else {
        Write-Host ('  [{0}] [跳过]   {1}' -f ($i + 1), $f.Label) -ForegroundColor Yellow
        Write-Host ('        {0}' -f $r.Report) -ForegroundColor DarkGray
        Write-Host ('        前置条件：{0}' -f $f.Require) -ForegroundColor DarkGray
    }
}

$usable = @($checks | Where-Object { $_.Ok })
if ($usable.Count -eq 0) {
    Write-Host ''
    Write-Host '没有任何功能可用：请先在对应桌面端登录一次，再重跑本向导。' -ForegroundColor Red
    Write-Host '（脚本只读取本机登录态，不会索要或保存你的账号密码）' -ForegroundColor DarkGray
    exit 1
}

if ($CheckOnly) {
    Write-Host ''
    Write-Host ('体检完成：{0}/{1} 个功能可用。未安装任何任务（-CheckOnly）。' -f $usable.Count, $features.Count) -ForegroundColor Cyan
    exit 0
}

# --- 3. 选择并安装 ---
Write-Host ''
Write-Host '[3/3] 选择要安装的功能 ...' -ForegroundColor Yellow
$selected = @()

if ($Only.Count -gt 0) {
    $known = @($features | ForEach-Object { $_.Key })
    foreach ($key in $Only) {
        $hit = @($checks | Where-Object { $_.Feature.Key -ieq $key.Trim() })
        if ($hit.Count -eq 0) {
            Write-Host ('  未知功能名：{0}（可用：{1}）' -f $key, ($known -join ', ')) -ForegroundColor Red
            exit 1
        }
        if (-not $hit[0].Ok) {
            Write-Host ('  {0} 体检未通过，不能安装：{1}' -f $hit[0].Feature.Label, $hit[0].Report) -ForegroundColor Red
            exit 1
        }
        $selected += $hit[0].Feature
    }
} elseif ($Yes) {
    $selected = @($usable | ForEach-Object { $_.Feature })
    Write-Host ('  非交互模式，安装全部可用功能：{0}' -f (($selected | ForEach-Object { $_.Key }) -join ', ')) -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host '  输入要安装的功能编号（多个用逗号隔开），直接回车 = 全部可用功能，q = 退出' -ForegroundColor White
    $answer = (Read-Host '  请选择').Trim()
    if ($answer -ieq 'q') { Write-Host '  已取消，未做任何改动。'; exit 0 }
    if (-not $answer) {
        $selected = @($usable | ForEach-Object { $_.Feature })
    } else {
        foreach ($part in ($answer -split '[,，\s]+')) {
            if (-not $part) { continue }
            $n = 0
            if (-not [int]::TryParse($part, [ref]$n) -or $n -lt 1 -or $n -gt $features.Count) {
                Write-Host ('  无效编号：{0}' -f $part) -ForegroundColor Red
                exit 1
            }
            $hit = @($checks | Where-Object { $_.Index -eq $n })[0]
            if (-not $hit.Ok) {
                Write-Host ('  编号 {0} 体检未通过，不能安装：{1}' -f $n, $hit.Report) -ForegroundColor Red
                exit 1
            }
            $selected += $hit.Feature
        }
        $selected = @($selected | Sort-Object Key -Unique)
    }
}

if ($selected.Count -eq 0) { Write-Host '  未选择任何功能，退出。'; exit 0 }

Write-Host ''
Write-Host ('即将安装：{0}' -f (($selected | ForEach-Object { $_.Label }) -join '、')) -ForegroundColor Cyan
if (-not $Yes) {
    $confirm = (Read-Host '  确认注册计划任务？[Y/n]').Trim()
    if ($confirm -and $confirm -notin @('Y', 'y', 'yes', '是')) { Write-Host '  已取消，未做任何改动。'; exit 0 }
}

$failed = @()
foreach ($f in $selected) {
    Write-Host ''
    Write-Host ('--- 安装 {0} ---' -f $f.Label) -ForegroundColor Cyan
    $installer = Join-Path $here $f.Installer
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw ('安装脚本退出码 {0}' -f $LASTEXITCODE) }
    } catch {
        $failed += $f.Label
        Write-Host ('  安装失败：{0}' -f $_.Exception.Message) -ForegroundColor Red
        Write-Host '  若是「拒绝访问 / 0x80070005」，请用同一 Windows 账号以管理员身份打开 PowerShell 重跑。' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host ('=' * 52) -ForegroundColor DarkGray
if ($failed.Count -eq 0) {
    Write-Host '全部安装完成。' -ForegroundColor Green
} else {
    Write-Host ('部分安装失败：{0}' -f ($failed -join '、')) -ForegroundColor Red
}
Write-Host '验证（不领取，只看状态）：' -ForegroundColor DarkGray
foreach ($f in $selected) {
    Write-Host ('  python "{0}" status' -f (Join-Path $here $f.Doctor)) -ForegroundColor DarkGray
    Write-Host ('  日志：{0}' -f (Join-Path $here $f.Log)) -ForegroundColor DarkGray
}
Write-Host ''
Write-Host '以后想卸载：powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Uninstall' -ForegroundColor DarkGray
Write-Host '以后想更新：重新解压新版 zip 覆盖本目录，再重跑一次本向导（同名任务会被更新）。' -ForegroundColor DarkGray
Write-Host ''

if ($failed.Count -gt 0) { exit 1 }
exit 0
