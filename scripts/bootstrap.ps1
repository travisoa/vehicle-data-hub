# Windows 引导脚本（bootstrap.sh 的 PowerShell 版）
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
#   $env:INSTALL_DEV=1; $env:INSTALL_PLAYWRIGHT=1; powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RootDir

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"

Write-Host "[bootstrap] using python: $PythonBin"
& $PythonBin -m venv .venv

Write-Host "[bootstrap] upgrading pip"
& $VenvPython -m pip install --upgrade pip

Write-Host "[bootstrap] installing base requirements"
& $VenvPython -m pip install -r requirements.txt

if ($env:INSTALL_PLAYWRIGHT -eq "1") {
    Write-Host "[bootstrap] installing browser requirements + Playwright Chromium"
    & $VenvPython -m pip install -r requirements-browser.txt
    & $VenvPython -m playwright install chromium
} else {
    Write-Host "[bootstrap] skip browser install (set INSTALL_PLAYWRIGHT=1 to enable)"
}

if ($env:INSTALL_DEV -eq "1") {
    Write-Host "[bootstrap] installing dev requirements (pytest/ruff)"
    & $VenvPython -m pip install -r requirements-dev.txt
} else {
    Write-Host "[bootstrap] skip dev install (set INSTALL_DEV=1 to enable)"
}

# 目录同步需要 LibreOffice 把 .doc 转成 .docx；Windows 版安装后默认不在 PATH，
# 代码会自动探测 Program Files 下的默认路径
$soffice = @(
    "C:\Program Files\LibreOffice\program\soffice.exe",
    "C:\Program Files (x86)\LibreOffice\program\soffice.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($soffice) {
    Write-Host "[bootstrap] LibreOffice found: $soffice"
} else {
    Write-Host "[bootstrap] 未检测到 LibreOffice。" -ForegroundColor Yellow
    Write-Host "[bootstrap] 其余功能不受影响；仅 'gonggao jianmian sync' 需要它来转换目录附件。" -ForegroundColor Yellow
    Write-Host "[bootstrap] 可用 winget install TheDocumentFoundation.LibreOffice 安装。" -ForegroundColor Yellow
}

Write-Host "[bootstrap] done"
Write-Host "[bootstrap] run with: .venv\Scripts\python.exe main.py --help"
