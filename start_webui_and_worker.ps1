param(
    [int]$Port = 8080,
    [int]$WorkerConcurrency = 2,
    [switch]$Reload,
    [switch]$OpenBrowser,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Resolve-RunnerInfo {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        return @{
            Name = "uv"
            ApiCommand = "uv run uvicorn api.main:app --host 0.0.0.0 --port $Port"
            WorkerCommand = "uv run python structured_worker.py --max-concurrency $WorkerConcurrency"
        }
    }

    $venvPython = Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"
    if (Test-Path $venvPython) {
        return @{
            Name = "python"
            ApiCommand = "`"$venvPython`" -m uvicorn api.main:app --host 0.0.0.0 --port $Port"
            WorkerCommand = "`"$venvPython`" structured_worker.py --max-concurrency $WorkerConcurrency"
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            Name = "python"
            ApiCommand = "python -m uvicorn api.main:app --host 0.0.0.0 --port $Port"
            WorkerCommand = "python structured_worker.py --max-concurrency $WorkerConcurrency"
        }
    }

    throw "uv or python was not found. Please install the runtime first."
}

function Start-RoleWindow {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,
        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    Write-Host "Starting window: $Title"
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        $Command
    ) -WorkingDirectory $ProjectRoot | Out-Null
}

$runner = Resolve-RunnerInfo
$apiCommand = $runner.ApiCommand
if ($Reload) {
    $apiCommand += " --reload"
}
$workerCommand = $runner.WorkerCommand

Write-Host "Project root: $ProjectRoot"
Write-Host "Runner: $($runner.Name)"
Write-Host "API command: $apiCommand"
Write-Host "Worker command: $workerCommand"
Write-Host "WebUI URL: http://localhost:$Port"

if ($DryRun) {
    Write-Host "DryRun mode, no process started."
    exit 0
}

Start-RoleWindow -Title "MediaCrawler API" -Command $apiCommand
Start-Sleep -Milliseconds 600
Start-RoleWindow -Title "MediaCrawler Worker" -Command $workerCommand

if ($OpenBrowser) {
    Start-Process "http://localhost:$Port" | Out-Null
}

Write-Host "API and worker started."
Write-Host "Close the spawned PowerShell windows to stop them."
