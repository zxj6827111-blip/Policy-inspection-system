$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundledPython = 'C:\Users\zxj68\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { 'python' }
Set-Location $root
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $python -m venv .venv
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& '.\.venv\Scripts\python.exe' -m playwright install chromium
Write-Host '安装完成。运行 .\run.ps1，然后打开 http://127.0.0.1:8765'

