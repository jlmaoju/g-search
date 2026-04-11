param(
  [string]$ZhipuApiKey = ""
)

$Host.UI.RawUI.WindowTitle = "Gcores Search Preview Server Refresh"
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location -Path $workspace

$containerName = "gcores-qdrant-preview"
$qdrantUrl = "http://127.0.0.1:6335"
$collectionName = "gcores_memory_preview_full_v1"
$pythonExe = Join-Path $env:USERPROFILE "anaconda3\python.exe"
if (-not (Test-Path $pythonExe)) {
  $pythonExe = "python"
}

function Resolve-PreferredEnvValue {
  param([string]$Name)

  foreach ($scope in @("Process", "User", "Machine")) {
    $value = [Environment]::GetEnvironmentVariable($Name, $scope)
    if (-not [string]::IsNullOrWhiteSpace($value)) {
      return $value
    }
  }
  return ""
}

$resolvedZhipuApiKey = $ZhipuApiKey
if (-not $resolvedZhipuApiKey) {
  $resolvedZhipuApiKey = Resolve-PreferredEnvValue -Name "ZHIPU_API_KEY"
}
if ([string]::IsNullOrWhiteSpace($resolvedZhipuApiKey)) {
  throw "Missing ZHIPU_API_KEY. Set it in the environment or pass -ZhipuApiKey."
}
$env:ZHIPU_API_KEY = $resolvedZhipuApiKey

function Ensure-DockerReady {
  docker version *> $null
  if ($LASTEXITCODE -eq 0) {
    return
  }
  $dockerDesktop = Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $dockerDesktop) {
    Start-Process $dockerDesktop
  }
  for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    docker version *> $null
    if ($LASTEXITCODE -eq 0) {
      return
    }
  }
  throw "Docker daemon did not become ready in time. Start Docker Desktop manually if needed."
}

function Ensure-QdrantContainer {
  $storage = Join-Path $workspace "data\qdrant_ui_server_storage"
  if (-not (Test-Path $storage)) {
    New-Item -ItemType Directory -Path $storage | Out-Null
  }
  $running = docker ps --filter "name=$containerName" --format "{{.Names}}"
  if ($running -contains $containerName) {
    return
  }
  $existing = docker ps -a --filter "name=$containerName" --format "{{.Names}}"
  if ($existing -contains $containerName) {
    docker start $containerName | Out-Null
  } else {
    docker run -d --name $containerName -p 6335:6333 -v "${storage}:/qdrant/storage" qdrant/qdrant:latest | Out-Null
  }
  for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 2
    try {
      $resp = Invoke-WebRequest -UseBasicParsing "$qdrantUrl/collections" -TimeoutSec 5
      if ($resp.StatusCode -eq 200) {
        return
      }
    } catch {
    }
  }
  throw "Qdrant preview server did not become ready in time."
}

Write-Host "[Preview Server Refresh] Starting..."
Write-Host "Working directory: $PWD"
Write-Host ""

Ensure-DockerReady
Ensure-QdrantContainer

& $pythonExe (Join-Path $workspace "scripts\build_preview_qdrant_from_sqlite.py") `
  --root data `
  --target-url $qdrantUrl `
  --collection-name $collectionName `
  --reset

$exitCode = $LASTEXITCODE
Write-Host ""
Write-Host "[Preview Server Refresh] Exited with code $exitCode."
Pause
