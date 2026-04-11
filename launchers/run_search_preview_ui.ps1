param(
  [string]$ZhipuApiKey = ""
)

$Host.UI.RawUI.WindowTitle = "Gcores Search Preview UI"
$workspace = Split-Path -Parent $PSScriptRoot
Set-Location -Path $workspace

$containerName = "gcores-qdrant-preview"
$qdrantUrl = "http://127.0.0.1:6335"
$preferredCollections = @("gcores_memory_preview_full_v1", "gcores_memory_preview_v1")
$collectionName = $preferredCollections[0]
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

function Test-PortAvailable {
  param([int]$Port)
  $listener = $null
  try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $Port)
    $listener.Start()
    return $true
  } catch {
    return $false
  } finally {
    if ($listener) {
      $listener.Stop()
    }
  }
}

function Get-ExistingPreviewPort {
  $existing = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*serve-search*' -and (
      $_.CommandLine -like '*gcores_memory_preview_full_v1*' -or
      $_.CommandLine -like '*gcores_memory_preview_v1*'
    )
  }
  foreach ($proc in $existing) {
    if ($proc.CommandLine -match '--port\s+(\d+)') {
      return [int]$Matches[1]
    }
  }
  return $null
}

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

function Test-PreviewCollectionExists {
  param([string]$Name)
  try {
    $resp = Invoke-WebRequest -UseBasicParsing "$qdrantUrl/collections/$Name" -TimeoutSec 5
    return $resp.StatusCode -eq 200
  } catch {
    return $false
  }
}

$existingPort = Get-ExistingPreviewPort
if ($existingPort) {
  Write-Host "[Search Preview UI] Existing preview detected on http://127.0.0.1:$existingPort"
  Start-Process "http://127.0.0.1:$existingPort"
  exit 0
}

Write-Host "[Search Preview UI] Starting..."
Write-Host "Working directory: $PWD"
Write-Host ""

Ensure-DockerReady
Ensure-QdrantContainer

foreach ($name in $preferredCollections) {
  if (Test-PreviewCollectionExists -Name $name) {
    $collectionName = $name
    break
  }
}

if (-not (Test-PreviewCollectionExists -Name $collectionName)) {
  Write-Host "[Search Preview UI] Preview collection missing. Running refresh first..."
  powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "refresh_search_preview_server.ps1")
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[Search Preview UI] Refresh failed."
    Pause
    exit 1
  }
  if (-not (Test-PreviewCollectionExists -Name $collectionName)) {
    foreach ($name in $preferredCollections) {
      if (Test-PreviewCollectionExists -Name $name) {
        $collectionName = $name
        break
      }
    }
  }
}

$candidatePorts = 9000..9010
$port = $null
foreach ($candidate in $candidatePorts) {
  if (Test-PortAvailable -Port $candidate) {
    $port = $candidate
    break
  }
}
if (-not $port) {
  Write-Host "[Search Preview UI] No free preview port found in 9000-9010."
  Pause
  exit 1
}

Write-Host "[Search Preview UI] URL: http://127.0.0.1:$port"
Write-Host ""

Start-Process "http://127.0.0.1:$port"

& $pythonExe -m gcores_crawler serve-search `
  --output-dir data `
  --qdrant-url $qdrantUrl `
  --collection-name $collectionName `
  --host 127.0.0.1 `
  --port $port

$exitCode = $LASTEXITCODE
Write-Host ""
Write-Host "[Search Preview UI] Exited with code $exitCode."
Pause
