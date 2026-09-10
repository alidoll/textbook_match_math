# 打包教材匹配系统为 Windows 桌面应用
# 用法：powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1

$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."

Write-Host "=== 安装打包依赖 ===" -ForegroundColor Cyan
pip install pyinstaller pywebview

Write-Host "=== 开始打包 ===" -ForegroundColor Cyan
Push-Location $root
pyinstaller desktop/textbook-match.spec --noconfirm
Pop-Location

$exe = "$root\dist\TextbookMatch\TextbookMatch.exe"
# Entry: desktop/launch.py
if (Test-Path $exe) {
    Write-Host "=== 打包成功 ===" -ForegroundColor Green
    Write-Host "可执行文件：$exe"
} else {
    Write-Host "=== 打包失败，请检查输出 ===" -ForegroundColor Red
    exit 1
}
