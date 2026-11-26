# Builds NoVmp.exe and vtildump.exe (64-bit) in novmp\build. Needs Visual Studio 2022 (C++), CMake 3.20+,
# Ninja and git (VTIL fetches Capstone and Keystone; linux-pe is downloaded at a pinned hash).
#
#   powershell -File novmp\build.ps1
$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'no Visual Studio with the C++ tools' }
$vcvarsall = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
$env:PATH = (Split-Path $vswhere) + ';' + $env:PATH
$build = Join-Path $PSScriptRoot 'build'
$script = Join-Path ([IO.Path]::GetTempPath()) 'fathom-novmp.cmd'
Set-Content -Encoding ascii $script @(
    "@call `"$vcvarsall`" x64 >nul || exit /b 1",
    "@cmake -S `"$PSScriptRoot`" -B `"$build`" -G Ninja -DCMAKE_BUILD_TYPE=Release || exit /b 1",
    "@cmake --build `"$build`" --target NoVmp vtildump || exit /b 1"
)
cmd /c "`"$script`""
$code = $LASTEXITCODE
Remove-Item $script -ErrorAction SilentlyContinue
if ($code -ne 0) { throw 'the NoVmp build failed' }
"built $build\NoVmp.exe and vtildump.exe"
