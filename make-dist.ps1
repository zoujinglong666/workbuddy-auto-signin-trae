# make-dist.ps1 — 生成给同事的干净分发包（Windows）
#
# 用法：powershell -ExecutionPolicy Bypass -File .\make-dist.ps1
# 产物：dist\workbuddy-auto-signin-YYYYMMDD.zip
#
# 采用「白名单」而非「排除法」：只有下面列出的文件会进 zip。
# 本机的日志、token 缓存、设备号、当日状态、__pycache__、.git 一律不会被打包。
# 打包前还会扫描一遍真实凭据特征，命中即中止，避免把登录态误发出去。

param(
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (-not $OutDir) { $OutDir = Join-Path $here 'dist' }

# ── 白名单：只有这些文件会进入分发包 ──
$include = @(
    'README.md', 'LICENSE', '.gitignore',
    'signin.py', 'qoder_signin.py', 'trae_signin.py', 'minmax_signin.py',
    'install-windows.ps1', 'install-qoder-windows.ps1', 'install-trae-windows.ps1',
    'install-minimax-windows.ps1',
    'setup-windows.ps1', 'setup.cmd', 'make-dist.ps1',
    'install-qoder-macos.sh', 'uninstall-qoder-macos.sh',
    'workbuddy-auto-signin.plist.example',
    'trae/NOTICE.md', 'trae/trae_checkin.py', 'trae/trae_get_token.py',
    'runtime/python-3.11.9-embed-amd64.zip'
)

# ── 敏感特征：任何白名单文件里出现真实凭据就中止打包 ──
$secretPattern = [regex]'(?i)(eyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{24,}\.[0-9a-f]{16}\b|Bearer\s+[A-Za-z0-9._-]{40,})'

Write-Host ''
Write-Host '生成同事分发包' -ForegroundColor Cyan
Write-Host ('-' * 52) -ForegroundColor DarkGray

# --- 1. 文件存在性 + 敏感内容扫描 ---
Write-Host '[1/4] 校验白名单文件 ...' -NoNewline
$missing = @()
foreach ($rel in $include) {
    if (-not (Test-Path -LiteralPath (Join-Path $here $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    Write-Host ' 失败' -ForegroundColor Red
    throw ('分发所需文件缺失：{0}' -f ($missing -join '、'))
}
Write-Host (' OK（{0} 个文件）' -f $include.Count) -ForegroundColor Green

Write-Host '[2/4] 扫描是否混入真实凭据 ...' -NoNewline
$leaks = @()
foreach ($rel in $include) {
    $path = Join-Path $here $rel
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -gt 4MB) { continue }   # 源码都是文本，超 4MB 不扫（也不该有）
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($secretPattern.IsMatch($text)) { $leaks += $rel }
}
if ($leaks.Count -gt 0) {
    Write-Host ' 中止' -ForegroundColor Red
    throw ('以下文件里疑似含有真实 token，请先清理再打包：{0}' -f ($leaks -join '、'))
}
Write-Host ' 干净' -ForegroundColor Green

# 顺带列一下「本机有、但不会进包」的运行文件，让你确认
$runtime = @(Get-ChildItem -LiteralPath $here -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -notlike '*\.git\*' -and (
            $_.Extension -eq '.log' -or
            $_.Name -like '.trae_*' -or
            $_.Name -like '.trae-*.json' -or
            $_.Name -eq 'state.vscdb' -or
            $_.DirectoryName -like '*__pycache__*'
        )
    })
if ($runtime.Count -gt 0) {
    Write-Host ('       已排除 {0} 个本机运行文件（日志 / token 缓存 / 状态 / 缓存）' -f $runtime.Count) -ForegroundColor DarkGray
}

# --- 3. 组装临时目录并压缩 ---
Write-Host '[3/4] 组装分发目录 ...' -NoNewline
$stage = Join-Path $env:TEMP ('signin-dist-' + [guid]::NewGuid().ToString('N'))
$pkg = Join-Path $stage 'workbuddy-auto-signin'
try {
    New-Item -ItemType Directory -Path $pkg -Force | Out-Null
    foreach ($rel in $include) {
        $src = Join-Path $here ($rel -replace '/', '\')
        $dst = Join-Path $pkg ($rel -replace '/', '\')
        $parent = Split-Path -Parent $dst
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }

    if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
    $zip = Join-Path $OutDir ('workbuddy-auto-signin-' + (Get-Date -Format 'yyyyMMdd') + '.zip')
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Compress-Archive -Path $pkg -DestinationPath $zip -CompressionLevel Optimal -Force
    Write-Host ' OK' -ForegroundColor Green

    # --- 4. 回读 zip 验证内容 ---
    Write-Host '[4/4] 回读校验压缩包 ...' -NoNewline
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        $forbidden = @($names | Where-Object {
            $_ -match '\.log$|/\.git/|__pycache__|\.trae_(checkin_state|token_cache|device_ids)\.json|/\.workbuddy/'
        })
        if ($forbidden.Count -gt 0) { throw ('压缩包里出现不该分发的文件：{0}' -f ($forbidden -join '、')) }
        if ($names.Count -lt $include.Count) { throw '压缩包文件数少于白名单，可能被压缩工具漏掉' }
    } finally {
        $archive.Dispose()
    }
    Write-Host (' OK（{0} 个条目）' -f $names.Count) -ForegroundColor Green

    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
    $size = [Math]::Round((Get-Item -LiteralPath $zip).Length / 1KB, 1)
    Write-Host ''
    Write-Host '分发包已生成：' -ForegroundColor Green
    Write-Host ('  {0}  ({1} KB)' -f $zip, $size) -ForegroundColor Cyan
    Write-Host ('  SHA256: {0}' -f $hash) -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '给同事的一句话：' -ForegroundColor White
    Write-Host '  解压整个 zip（保留 trae 子目录）→ 双击 setup.cmd → 按提示选功能。' -ForegroundColor Yellow
    Write-Host '  无需安装 Python（包内已内置），但需登录过对应的桌面端客户端。' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '提醒：别直接压缩整个工作目录，会把本机日志和 token 缓存一起发出去。' -ForegroundColor Yellow
    Write-Host ''
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
