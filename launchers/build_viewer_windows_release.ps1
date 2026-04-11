param(
  [string]$PythonExe = "python",
  [string]$SourceRoot = "data",
  [string]$CollectionName = "gcores_memory_release_v3",
  [string]$QdrantPath = "qdrant",
  [string]$QdrantDockerContainer = "gcores-qdrant-release",
  [string]$EmbeddingProvider = "zhipu",
  [string]$EmbeddingModel = "embedding-3",
  [string]$FallbackEmbeddingApiKey = "",
  [string]$TargetPlatform = "win-x64",
  [string]$QdrantVersion = "v1.17.1",
  [string]$OutputRoot = "",
  [switch]$SkipDataBundle,
  [switch]$SkipAppBuild,
  [switch]$SkipZip,
  [switch]$ReuseBuildEnv
)

$ErrorActionPreference = "Stop"

function Ensure-Dir([string]$PathValue) {
  if (-not (Test-Path -LiteralPath $PathValue)) {
    New-Item -ItemType Directory -Path $PathValue -Force | Out-Null
  }
}

function Reset-Dir([string]$PathValue) {
  if (Test-Path -LiteralPath $PathValue) {
    Remove-Item -LiteralPath $PathValue -Recurse -Force
  }
  New-Item -ItemType Directory -Path $PathValue -Force | Out-Null
}

function Run-Step([string]$Label, [scriptblock]$Action) {
  Write-Host "[Viewer Release] $Label" -ForegroundColor Cyan
  & $Action
}

function Assert-LastExit([string]$Label) {
  if ($LASTEXITCODE -ne 0) {
    throw "[Viewer Release] $Label failed with exit code $LASTEXITCODE"
  }
}

function Resolve-QdrantSourcePath([string]$ProjectRoot, [string]$SourceRoot, [string]$ConfiguredPath) {
  if ([System.IO.Path]::IsPathRooted($ConfiguredPath)) {
    $candidate = $ConfiguredPath
  } else {
    $candidate = Join-Path $SourceRoot $ConfiguredPath
  }
  if ((Test-Path -LiteralPath $candidate) -and (Get-ChildItem -LiteralPath $candidate -Force | Select-Object -First 1)) {
    return [System.IO.Path]::GetFullPath($candidate)
  }
  return $null
}

function Test-QdrantCollectionPresent([string]$StorageRoot, [string]$CollectionName) {
  if (-not $StorageRoot -or -not (Test-Path -LiteralPath $StorageRoot)) {
    return $false
  }
  $candidateDirs = @(
    (Join-Path $StorageRoot "collections\\$CollectionName"),
    (Join-Path $StorageRoot "collection\\$CollectionName")
  )
  foreach ($candidate in $candidateDirs) {
    if (Test-Path -LiteralPath $candidate) {
      return $true
    }
  }
  $metaPath = Join-Path $StorageRoot "meta.json"
  if (-not (Test-Path -LiteralPath $metaPath)) {
    return $false
  }
  try {
    $meta = Get-Content -LiteralPath $metaPath -Raw | ConvertFrom-Json
    if ($meta.collections -and $meta.collections.PSObject.Properties.Name -contains $CollectionName) {
      return $true
    }
  } catch {
    return $false
  }
  return $false
}

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) {
  $OutputRoot = Join-Path $projectRoot "dist\\viewer-release"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)

$buildRoot = Join-Path $projectRoot ".build\\viewer-win"
$buildDistRoot = Join-Path $buildRoot "dist"
$buildWorkRoot = Join-Path $buildRoot "work"
$buildSpecRoot = Join-Path $buildRoot "spec"
$venvRoot = Join-Path $projectRoot ".venv-viewer-build"
$venvPython = Join-Path $venvRoot "Scripts\\python.exe"

$appDir = Join-Path $OutputRoot "viewer-app-win-x64"
$dataDir = Join-Path $OutputRoot "viewer-data-win-x64"
$dataCurrentDir = Join-Path $dataDir "data\\current"
$runnableDir = Join-Path $OutputRoot "viewer-win-x64-runnable"
$qdrantVendorDir = Join-Path $appDir "vendor\\qdrant"
$fallbackKeyPath = Join-Path $appDir "config\\secrets\\default_embedding_api_key.txt"

$entrySpec = Join-Path $projectRoot "packaging\\gsearch_viewer_win.spec"
$downloadQdrantScript = Join-Path $projectRoot "scripts\\download_qdrant_release.py"
$appReadmePath = Join-Path $appDir "README.txt"
$runViewerBatPath = Join-Path $appDir "run_viewer.bat"
$resolvedSourceRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $SourceRoot))
$resolvedQdrantPath = Resolve-QdrantSourcePath -ProjectRoot $projectRoot -SourceRoot $resolvedSourceRoot -ConfiguredPath $QdrantPath
$copiedQdrantRoot = $null

if ($resolvedQdrantPath -and -not (Test-QdrantCollectionPresent -StorageRoot $resolvedQdrantPath -CollectionName $CollectionName)) {
  Write-Host ("[Viewer Release] Ignoring local Qdrant path without collection {0}: {1}" -f $CollectionName, $resolvedQdrantPath) -ForegroundColor Yellow
  $resolvedQdrantPath = $null
}

Reset-Dir $OutputRoot
Reset-Dir $buildRoot
Ensure-Dir $dataCurrentDir

if (-not $resolvedQdrantPath) {
  $copiedQdrantRoot = Join-Path $buildRoot "qdrant-storage-copy"
  Ensure-Dir $copiedQdrantRoot
  Run-Step "Copy Qdrant storage from docker container $QdrantDockerContainer" {
    & docker cp "${QdrantDockerContainer}:/qdrant/storage/." $copiedQdrantRoot
    Assert-LastExit "docker cp qdrant storage"
  }
  $resolvedQdrantPath = $copiedQdrantRoot
}

if (-not $SkipDataBundle) {
  Run-Step "Export viewer data bundle" {
    & $PythonExe -m gcores_crawler export-viewer-bundle `
      --source-root $resolvedSourceRoot `
      --output-dir $dataCurrentDir `
      --qdrant-path $resolvedQdrantPath `
      --collection-name $CollectionName `
      --embedding-provider $EmbeddingProvider `
      --embedding-model $EmbeddingModel `
      --target-platform $TargetPlatform `
      --overwrite
    Assert-LastExit "export-viewer-bundle"
  }
} else {
  Write-Host "[Viewer Release] Skipping data bundle export." -ForegroundColor Yellow
}

$manifestPath = Join-Path $dataCurrentDir "manifest.json"
$manifest = $null
$dataVersionSlug = "data"
if (Test-Path -LiteralPath $manifestPath) {
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $dataVersionSlug = ([string]$manifest.data_version) -replace "[^0-9A-Za-z._-]", "_"
}

if ((-not $ReuseBuildEnv) -and (Test-Path -LiteralPath $venvRoot)) {
  Remove-Item -LiteralPath $venvRoot -Recurse -Force
}

if (-not $SkipAppBuild) {
  if (-not (Test-Path -LiteralPath $venvPython)) {
    Run-Step "Create isolated build venv" {
      & $PythonExe -m venv $venvRoot
      Assert-LastExit "python -m venv"
    }
  }

  Run-Step "Install build dependencies" {
    & $venvPython -m pip install --upgrade pip setuptools wheel
    Assert-LastExit "pip bootstrap"
    & $venvPython -m pip install pyinstaller fastapi uvicorn qdrant-client
    Assert-LastExit "pip install runtime/build deps"
  }

  Run-Step "Build viewer app with PyInstaller" {
    & $venvPython -m PyInstaller `
      $entrySpec `
      --noconfirm `
      --clean `
      --distpath $buildDistRoot `
      --workpath $buildWorkRoot
    Assert-LastExit "PyInstaller build"
  }

  Run-Step "Copy app payload" {
    Copy-Item -LiteralPath (Join-Path $buildDistRoot "GSearchViewer") -Destination $appDir -Recurse -Force
    Ensure-Dir (Join-Path $appDir "data\\current")
  }

  Run-Step "Download bundled Qdrant binary" {
    & $PythonExe $downloadQdrantScript `
      --version $QdrantVersion `
      --platform $TargetPlatform `
      --output-dir $qdrantVendorDir `
      --overwrite
    Assert-LastExit "download_qdrant_release.py"
  }

  Run-Step "Write app launcher and README" {
    $readme = @"
GSearch Viewer (Windows)
========================

1. Extract the viewer app zip.
2. Extract the viewer data zip into the same folder so that data\\current\\manifest.json exists next to GSearchViewer.exe.
3. Double-click run_viewer.bat.
4. If search reports a missing API key, open Settings in the Viewer and paste a Zhipu API key once.

To update the database later:
- Close the Viewer.
- Replace the contents under `data\\current\\`.
- Launch again.
"@
    [System.IO.File]::WriteAllText($appReadmePath, $readme, (New-Object System.Text.UTF8Encoding($false)))

    $launcher = @"
@echo off
setlocal
title GSearch Viewer
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%data\current\manifest.json" (
  echo [GSearchViewer] Missing data package. Extract the viewer-data archive into this folder first.
  pause
  exit /b 1
)
"%APP_DIR%GSearchViewer.exe" --port 8766 --qdrant-url http://127.0.0.1:6341
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
  echo.
  echo [GSearchViewer] Viewer exited with code %EXITCODE%.
  pause
)
"@
    [System.IO.File]::WriteAllText($runViewerBatPath, $launcher, (New-Object System.Text.UTF8Encoding($false)))
  }

  if ($FallbackEmbeddingApiKey) {
    Run-Step "Write fallback embedding API key" {
      $fallbackDir = Split-Path -Parent $fallbackKeyPath
      Ensure-Dir $fallbackDir
      [System.IO.File]::WriteAllText($fallbackKeyPath, $FallbackEmbeddingApiKey.Trim(), (New-Object System.Text.UTF8Encoding($false)))
    }
  }
} else {
  Write-Host "[Viewer Release] Skipping app build." -ForegroundColor Yellow
}

if ((Test-Path -LiteralPath $appDir) -and (Test-Path -LiteralPath $dataCurrentDir)) {
  Run-Step "Create merged runnable folder" {
    Reset-Dir $runnableDir
    Copy-Item -Path (Join-Path $appDir "*") -Destination $runnableDir -Recurse -Force
    Ensure-Dir (Join-Path $runnableDir "data\\current")
    Copy-Item -Path (Join-Path $dataCurrentDir "*") -Destination (Join-Path $runnableDir "data\\current") -Recurse -Force
  }
}

$summary = [ordered]@{
  app_dir = $appDir
  data_dir = $dataDir
  runnable_dir = if (Test-Path -LiteralPath $runnableDir) { $runnableDir } else { $null }
  manifest_path = $manifestPath
  data_version = if ($manifest) { $manifest.data_version } else { $null }
  target_platform = $TargetPlatform
}

if ((-not $SkipZip) -and ((Test-Path -LiteralPath $appDir) -or (Test-Path -LiteralPath $dataDir))) {
  $appVersionSlug = if ($manifest) { [string]$manifest.app_min_version } else { "app" }
  $appZip = Join-Path $OutputRoot ("viewer-app-win-x64-{0}.zip" -f $appVersionSlug)
  $dataZip = Join-Path $OutputRoot ("viewer-data-win-x64-{0}.zip" -f $dataVersionSlug)
  if (Test-Path -LiteralPath $appZip) { Remove-Item -LiteralPath $appZip -Force }
  if (Test-Path -LiteralPath $dataZip) { Remove-Item -LiteralPath $dataZip -Force }

  if (Test-Path -LiteralPath $appDir) {
    Run-Step "Create app zip" {
      Compress-Archive -Path (Join-Path $appDir "*") -DestinationPath $appZip -Force
    }
    $summary["app_zip"] = $appZip
  }
  if (Test-Path -LiteralPath $dataDir) {
    Run-Step "Create data zip" {
      Compress-Archive -Path (Join-Path $dataDir "*") -DestinationPath $dataZip -Force
    }
    $summary["data_zip"] = $dataZip
  }
}

$summary | ConvertTo-Json -Depth 4
