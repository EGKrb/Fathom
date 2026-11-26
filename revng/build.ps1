# Builds rev.ng for Windows and stages it in revng\dist (see README.md): runs build.sh in MSYS2, which it finds in
# scoop (msys2) or in C:\msys64. The first build takes hours (LLVM 16 with clang and MLIR).
#
#   powershell -File revng\build.ps1 [-Work C:\Users\<you>\revng-build] [-Steps packages,fetch,libtcg,...]
param(
    [string] $Work = (Join-Path $env:USERPROFILE 'revng-build'),
    [string] $Steps = ''
)
$ErrorActionPreference = 'Stop'

$candidates = @(
    (Join-Path $env:USERPROFILE 'scoop\apps\msys2\current\usr\bin\bash.exe'),
    'C:\msys64\usr\bin\bash.exe'
)
$bash = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $bash) { throw 'MSYS2 not found (scoop install msys2, or https://www.msys2.org)' }

# everything through the environment: no quoting across PowerShell, cmd and bash
$env:REVNG_SCRIPT = Join-Path $PSScriptRoot 'build.sh'
$env:REVNG_WORK = $Work
$env:REVNG_STEPS = ($Steps -split '[,\s]+' | Where-Object { $_ }) -join ' '
$env:MSYSTEM = 'MSYS'
$env:CHERE_INVOKING = '1'
# (no double quotes in the command: Windows PowerShell mangles them for native programs)
& $bash -lc 'exec bash $(cygpath -u $REVNG_SCRIPT) $REVNG_STEPS'
if ($LASTEXITCODE -ne 0) { throw 'the rev.ng build failed' }
"rev.ng staged in $(Join-Path $PSScriptRoot 'dist')"
