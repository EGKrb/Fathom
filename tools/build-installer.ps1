<#
.SYNOPSIS
Builds the Fathom Windows installer (NSIS) from the staged release payload.

.DESCRIPTION
Prerequisites:
  - NSIS on the PATH (scoop install nsis, or https://nsis.sourceforge.io).
  - The release staged by tools\make-release.ps1, i.e. release\release\Fathom.exe
    exists (make-release leaves that folder behind after zipping).

Output: dist\Fathom_<Version>_setup.exe

.EXAMPLE
.\tools\make-release.ps1 -Version 1.1.0      # stages release\release\ (and the zips)
.\tools\build-installer.ps1 -Version 1.1.0   # -> dist\Fathom_1.1.0_setup.exe
#>
param(
    [string] $Version = '1.1.0'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$src = Join-Path $root 'release\release'
if (-not (Test-Path (Join-Path $src 'Fathom.exe'))) {
    throw "release payload not found ($src\Fathom.exe). Run tools\make-release.ps1 -Version $Version first."
}

$makensis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $makensis) { $makensis = Join-Path $env:USERPROFILE 'scoop\shims\makensis.exe' }
if (-not (Test-Path $makensis)) { throw "makensis not found. Install NSIS (scoop install nsis)." }

$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force $dist | Out-Null
$out = Join-Path $dist "Fathom_${Version}_setup.exe"

& $makensis /V2 `
    "/DVERSION=$Version" `
    "/DSRCDIR=$src" `
    "/DICON=$root\src\launcher\fathom.ico" `
    "/DLICENSE=$root\LICENSE" `
    "/DOUTFILE=$out" `
    (Join-Path $root 'tools\fathom-installer.nsi')
if ($LASTEXITCODE -ne 0) { throw "makensis failed ($LASTEXITCODE)" }

$sha = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLower()
$size = '{0:N1} MB' -f ((Get-Item $out).Length / 1MB)
Write-Host "installer: $out  ($size)  sha256 $sha"
