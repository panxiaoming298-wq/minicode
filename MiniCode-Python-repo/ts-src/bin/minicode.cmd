@echo off
setlocal

:: MiniCode Windows 启动脚本
:: 此脚本用于在 Windows CMD 和 PowerShell 中启动 minicode

:: 获取脚本所在目录的父目录（项目根目录）
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."

:: 使用 tsx 运行 TypeScript 入口文件
node "%PROJECT_ROOT%\node_modules\tsx\dist\cli.mjs" "%PROJECT_ROOT%\src\index.ts" %*

exit /b %ERRORLEVEL%
