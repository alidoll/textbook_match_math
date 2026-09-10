# 启动开发服务器（自动使用 .venv，无需手动 Activate.ps1）
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\dev-server.ps1")
