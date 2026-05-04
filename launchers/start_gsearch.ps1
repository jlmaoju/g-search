param(
  [string]$DataDir = "data",
  [string]$HostAddress = "127.0.0.1",
  [int]$Port = 8765,
  [string]$QdrantPort = "6336",
  [string]$CollectionName = "",
  [switch]$SkipQdrant,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location -Path $workspace

function Read-DotEnv {
  param([string]$Path)
  if (-not (Test-Path $Path)) {
    return
  }
  foreach ($rawLine in (Get-Content -Path $Path)) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      continue
    }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2) {
      continue
    }
    $name = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"')
    if ($name) {
      [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
  }
}

function Resolve-Python {
  $venvPython = Join-Path $workspace ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    return $venvPython
  }
  return "python"
}

function Ensure-DotEnv {
  $envPath = Join-Path $workspace ".env"
  if (Test-Path $envPath) {
    return
  }
  Copy-Item -Path (Join-Path $workspace ".env.example") -Destination $envPath
  Write-Host ""
  Write-Host "Created .env from .env.example." -ForegroundColor Yellow
  Write-Host "For full semantic search, edit .env and fill this line:" -ForegroundColor Yellow
  Write-Host "  ZHIPU_API_KEY=your_key_here" -ForegroundColor Yellow
  Write-Host ""
}

function Test-Docker {
  docker version *> $null
  return $LASTEXITCODE -eq 0
}

function Ensure-Qdrant {
  param(
    [string]$StorageDir,
    [string]$Port
  )
  if (-not (Test-Docker)) {
    Write-Host "Docker is not running. Starting without Qdrant; searches will use degraded keyword fallback." -ForegroundColor Yellow
    return $false
  }
  if (-not (Test-Path $StorageDir)) {
    Write-Host "Qdrant storage not found at $StorageDir. Starting without Qdrant; searches will use degraded keyword fallback." -ForegroundColor Yellow
    return $false
  }

  $containerName = "gsearch-qdrant-release"
  $existing = docker ps -a --filter "name=^/$containerName$" --format "{{.Names}}"
  if ($existing -contains $containerName) {
    docker start $containerName | Out-Null
  } else {
    $storageMount = (Resolve-Path $StorageDir).Path.Replace("\", "/")
    docker run -d --name $containerName -p "127.0.0.1:${Port}:6333" -v "${storageMount}:/qdrant/storage" qdrant/qdrant:latest | Out-Null
  }

  $qdrantUrl = "http://127.0.0.1:$Port"
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
      $resp = Invoke-WebRequest -UseBasicParsing "$qdrantUrl/collections" -TimeoutSec 3
      if ($resp.StatusCode -eq 200) {
        return $true
      }
    } catch {
    }
  }
  Write-Host "Qdrant did not become ready in time. Starting with degraded keyword fallback." -ForegroundColor Yellow
  return $false
}

Ensure-DotEnv
Read-DotEnv -Path (Join-Path $workspace ".env")

if ([string]::IsNullOrWhiteSpace($env:ZHIPU_API_KEY)) {
  Write-Host ""
  Write-Host "ZHIPU_API_KEY is empty." -ForegroundColor Yellow
  Write-Host "The service can still start, but semantic search will degrade to keyword fallback." -ForegroundColor Yellow
  Write-Host "To enable full semantic search, edit .env and fill: ZHIPU_API_KEY=..." -ForegroundColor Yellow
  Write-Host ""
}

if (-not $CollectionName) {
  $CollectionName = if ($env:GCORES_QDRANT_COLLECTION) { $env:GCORES_QDRANT_COLLECTION } else { "gcores_memory_release_v3" }
}

$pythonExe = Resolve-Python
$resolvedDataDir = Join-Path $workspace $DataDir
if (-not (Test-Path (Join-Path $resolvedDataDir "catalog.sqlite"))) {
  throw "Missing database. Extract the data package so this file exists: $DataDir\catalog.sqlite"
}

$qdrantReady = $false
if (-not $SkipQdrant) {
  $qdrantReady = Ensure-Qdrant -StorageDir (Join-Path $resolvedDataDir "qdrant_release_storage") -Port $QdrantPort
}

$url = "http://${HostAddress}:$Port"
Write-Host ""
Write-Host "Starting G-Search on $url" -ForegroundColor Cyan
Write-Host "Data: $DataDir"
Write-Host "Collection: $CollectionName"
Write-Host "Qdrant: $(if ($qdrantReady) { "http://127.0.0.1:$QdrantPort" } else { "disabled / degraded fallback" })"
Write-Host ""

if (-not $NoBrowser) {
  Start-Process $url
}

$args = @(
  "-m", "gcores_crawler", "serve-search",
  "--output-dir", $DataDir,
  "--collection-name", $CollectionName,
  "--host", $HostAddress,
  "--port", "$Port"
)
if ($qdrantReady) {
  $args += @("--qdrant-url", "http://127.0.0.1:$QdrantPort")
} else {
  $args += @("--qdrant-path", "qdrant_empty")
}

& $pythonExe @args
exit $LASTEXITCODE
