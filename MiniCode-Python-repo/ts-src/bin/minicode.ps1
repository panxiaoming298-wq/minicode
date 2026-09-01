# MiniCode PowerShell 启动脚本
# 此脚本用于在 PowerShell 中启动 minicode

# 获取脚本所在目录的父目录（项目根目录）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")

# 使用 tsx 运行 TypeScript 入口文件
& node "$ProjectRoot\node_modules\tsx\dist\cli.mjs" "$ProjectRoot\src\index.ts" @args

exit $LASTEXITCODE
