param(
    [string]$Workspace = "",
    [int]$Port = 8765
)

$frontendRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$backendRoot = [System.IO.Path]::GetFullPath((Join-Path $frontendRoot "..\MiniCode-Python-repo"))
$frontendDist = Join-Path $frontendRoot "dist"

if (-not (Test-Path -LiteralPath (Join-Path $frontendDist "index.html") -PathType Leaf)) {
    throw "Frontend build not found. Run 'npm install' and 'npm run build' in $frontendRoot first."
}

if (-not (Test-Path -LiteralPath (Join-Path $backendRoot "pyproject.toml") -PathType Leaf)) {
    throw "MiniCode Python backend not found at $backendRoot"
}

if (-not $Workspace) {
    $Workspace = $backendRoot
}

Write-Host "MiniCode Web: http://127.0.0.1:$Port"
Write-Host "Workspace: $Workspace"
Write-Host "Press Ctrl+C to stop."

Push-Location $backendRoot
try {
    python -m minicode.web_api --host 127.0.0.1 --port $Port --workspace $Workspace --frontend $frontendDist
}
finally {
    Pop-Location
}
