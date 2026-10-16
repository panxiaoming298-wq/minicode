param(
    [string]$Workspace = "",
    [int]$Port = 8765,
    [ValidateSet("primary", "legacy")]
    [string]$Runtime = "primary",
    [ValidateSet("primary", "legacy")]
    [string]$UltraRuntime = "primary",
    [string]$DataRoot = "",
    [string]$StateRoot = "",
    [string]$TemporaryRoot = ""
)

$frontendRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$backendRoot = [System.IO.Path]::GetFullPath((Join-Path $frontendRoot "..\MiniCode-Python-repo"))
$frontendDist = Join-Path $frontendRoot "dist"

# Read saved locations even when this shell predates a data-directory move.
if (-not $StateRoot) {
    $StateRoot = $env:MINI_CODE_STATE_DIR
    if (-not $StateRoot) {
        $StateRoot = [Environment]::GetEnvironmentVariable("MINI_CODE_STATE_DIR", "User")
    }
}
if ($StateRoot) {
    if (-not [System.IO.Path]::IsPathRooted($StateRoot)) {
        throw "StateRoot must be an absolute directory."
    }
    $env:MINI_CODE_STATE_DIR = [System.IO.Path]::GetFullPath($StateRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $env:MINI_CODE_STATE_DIR "settings.json") -PathType Leaf)) {
        throw "StateRoot has no settings.json. Copy the existing user state before switching locations."
    }
}
if (-not $TemporaryRoot) {
    $TemporaryRoot = $env:MINI_CODE_TEMP_DIR
    if (-not $TemporaryRoot) {
        $TemporaryRoot = [Environment]::GetEnvironmentVariable("MINI_CODE_TEMP_DIR", "User")
    }
}
if ($TemporaryRoot) {
    if (-not [System.IO.Path]::IsPathRooted($TemporaryRoot)) {
        throw "TemporaryRoot must be an absolute directory."
    }
    $env:MINI_CODE_TEMP_DIR = [System.IO.Path]::GetFullPath($TemporaryRoot)
    New-Item -ItemType Directory -Path $env:MINI_CODE_TEMP_DIR -Force | Out-Null
    $env:TEMP = $env:MINI_CODE_TEMP_DIR
    $env:TMP = $env:MINI_CODE_TEMP_DIR
}
foreach ($variableName in @("PIP_CACHE_DIR", "NPM_CONFIG_CACHE", "PLAYWRIGHT_BROWSERS_PATH")) {
    $savedValue = [Environment]::GetEnvironmentVariable($variableName, "User")
    if ($savedValue) {
        [Environment]::SetEnvironmentVariable($variableName, $savedValue, "Process")
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendDist "index.html") -PathType Leaf)) {
    throw "Frontend build not found. Run 'npm install' and 'npm run build' in $frontendRoot first."
}

if (-not (Test-Path -LiteralPath (Join-Path $backendRoot "pyproject.toml") -PathType Leaf)) {
    throw "MiniCode Python backend not found at $backendRoot"
}

if (-not $Workspace) {
    $Workspace = $backendRoot
}

$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python environment not found. Create $backendRoot\.venv and install .[web,memory] there. Global Python will not be used."
}
if ($DataRoot) {
    if (-not [System.IO.Path]::IsPathRooted($DataRoot)) {
        throw "DataRoot must be an absolute directory."
    }
    $env:MINI_CODE_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
}

Write-Host "MiniCode Web: http://127.0.0.1:$Port"
Write-Host "Workspace: $Workspace"
Write-Host "Web runtime: Standard / Deep = $Runtime; Ultra = $UltraRuntime. Other entry points keep their configuration."
Write-Host "Ultra rollback: -UltraRuntime legacy (does not change Standard / Deep)."
if ($Runtime -eq "primary") {
    Write-Host "Default mode: Deep. Missing budgets: 256000 tokens / 600 seconds. Standard writes to the selected Local or Worktree directory."
}
Write-Host "Press Ctrl+C to stop."

Push-Location $backendRoot
try {
    & $python -m minicode.web_api --check --workspace $Workspace
    if ($LASTEXITCODE -ne 0) {
        throw "MiniCode dependency check failed. Install missing dependencies in the project .venv."
    }
    $memoryStatus = & $python -c 'import json,sys; from minicode.config import load_effective_settings; from minicode.paged_memory.config import PagedMemoryConfig; c=PagedMemoryConfig.from_sources(load_effective_settings(sys.argv[1]),cwd=sys.argv[1]); print(json.dumps(dict(active=c.enabled or c.shadow_write,backend=c.backend,artifact_store=c.artifact_store_type)))' $Workspace
    if ($LASTEXITCODE -ne 0) {
        throw "Could not load MiniCode memory configuration."
    }
    $memoryConfig = $memoryStatus | ConvertFrom-Json
    if ($memoryConfig.active) {
        Write-Host "Memory authority: $($memoryConfig.backend); artifacts: $($memoryConfig.artifact_store)."
        if ($memoryConfig.backend -eq "sqlite") {
            & $python -m minicode.paged_memory.admin --cwd $Workspace --use-settings init-db
            if ($LASTEXITCODE -ne 0) { throw "Local memory database initialization failed." }
        }
    }
    & $python -m minicode.web_api --host 127.0.0.1 --port $Port --workspace $Workspace --frontend $frontendDist --web-runtime $Runtime --web-ultra-runtime $UltraRuntime
}
finally {
    Pop-Location
}
