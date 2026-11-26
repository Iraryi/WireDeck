[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$PyInstaller = Join-Path $RepoRoot '.venv\Scripts\pyinstaller.exe'

if (-not (Test-Path $VenvPy)) {
    throw "Missing virtualenv Python at $VenvPy"
}

function Assert-UnderRepo {
    param([string]$TargetPath)
    $resolved = [System.IO.Path]::GetFullPath($TargetPath)
    $prefix = $RepoRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean unsafe path outside repo root: $resolved"
    }
    return $resolved
}

Write-Host "=== [1/6] Reading single-source metadata from src/wiredeck/version.py ==="
$metaJson = & $VenvPy -c "import json; from wiredeck import version; print(json.dumps({'name': version.APP_NAME, 'ver': version.APP_VERSION, 'tuple': list(version.APP_VERSION_TUPLE), 'id': version.APP_ID, 'pub': version.PUBLISHER}))"
$meta = $metaJson | ConvertFrom-Json
$AppVersion = [string]$meta.ver
$AppId = [string]$meta.id
$AppName = [string]$meta.name
$Publisher = [string]$meta.pub
$vt = $meta.tuple
Write-Host "Building $AppName v$AppVersion (AppId=$AppId)"

Write-Host "=== [2/6] Cleaning build/, dist/, release/ safely ==="
foreach ($rel in @('build', 'dist', 'release')) {
    $dir = Assert-UnderRepo (Join-Path $RepoRoot $rel)
    if (Test-Path $dir) {
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

Write-Host "=== [3/6] Generating icons and Windows PE version resource ==="
& $VenvPy (Join-Path $RepoRoot 'scripts\make_icons.py')
if ($LASTEXITCODE -ne 0) { throw "make_icons.py failed" }

$verInfoText = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($vt[0]), $($vt[1]), $($vt[2]), $($vt[3])),
    prodvers=($($vt[0]), $($vt[1]), $($vt[2]), $($vt[3])),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '$Publisher'),
          StringStruct('FileDescription', '$AppName - Gigabit Wired Console Streaming Assistant'),
          StringStruct('FileVersion', '$AppVersion'),
          StringStruct('InternalName', '$AppName'),
          StringStruct('OriginalFilename', '$AppName.exe'),
          StringStruct('ProductName', '$AppName'),
          StringStruct('ProductVersion', '$AppVersion')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[System.IO.File]::WriteAllText((Join-Path $RepoRoot 'build\version_info.txt'), $verInfoText, [System.Text.UTF8Encoding]::new($false))

Write-Host "=== [4/6] Running PyInstaller dual-EXE --onedir build ==="
Push-Location $RepoRoot
try {
    & $PyInstaller --noconfirm --clean (Join-Path $RepoRoot 'installer\wiredeck.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$DistAppDir = Join-Path $RepoRoot 'dist\WireDeck'
$GuiExe = Join-Path $DistAppDir 'WireDeck.exe'
$CliExe = Join-Path $DistAppDir 'wiredeck-cli.exe'
if (-not (Test-Path $GuiExe) -or -not (Test-Path $CliExe)) {
    throw "Missing built executables in $DistAppDir"
}

Write-Host "=== [5/6] Packaging Portable ZIP and Inno Setup 6 installer ==="
$ReleaseDir = Join-Path $RepoRoot 'release'
$PortableStage = Join-Path $RepoRoot 'build\portable_stage\WireDeck'
New-Item -ItemType Directory -Path $PortableStage -Force | Out-Null
Copy-Item -Path (Join-Path $DistAppDir '*') -Destination $PortableStage -Recurse -Force
Copy-Item -Path (Join-Path $RepoRoot 'installer\portable.mode.template') -Destination (Join-Path $PortableStage 'portable.mode') -Force

$PortableZip = Join-Path $ReleaseDir "WireDeck-v$AppVersion-win64-portable.zip"
Compress-Archive -Path $PortableStage -DestinationPath $PortableZip -Force

$artifacts = @($PortableZip)

if (-not $SkipInstaller) {
    $isccCandidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Iscc) {
        throw "Inno Setup 6 (ISCC.exe) not found. Install via: winget install --id JRSoftware.InnoSetup -e"
    }
    $IssFile = Join-Path $RepoRoot 'installer\WireDeck.iss'
    & $Iscc "/DAppVersion=$AppVersion" "/DAppId=$AppId" "/DSourceDir=$DistAppDir" "/DOutputDir=$ReleaseDir" $IssFile
    if ($LASTEXITCODE -ne 0) { throw "ISCC.exe failed with exit code $LASTEXITCODE" }
    $SetupExe = Join-Path $ReleaseDir "WireDeck-v$AppVersion-win64-setup.exe"
    if (-not (Test-Path $SetupExe)) { throw "Missing expected setup binary: $SetupExe" }
    $artifacts = @($SetupExe, $PortableZip)
}

Write-Host "=== [6/6] Computing SHA256SUMS.txt ==="
$hashLines = @()
foreach ($file in $artifacts) {
    $h = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    $name = [System.IO.Path]::GetFileName($file)
    $hashLines += "$h  $name"
}
$ShaFile = Join-Path $ReleaseDir 'SHA256SUMS.txt'
[System.IO.File]::WriteAllText($ShaFile, ($hashLines -join "`r`n") + "`r`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "=== BUILD COMPLETE ==="
Get-ChildItem $ReleaseDir | Format-Table Name, Length -AutoSize
Get-Content $ShaFile
