# Builds the Triton plugin (Triton.dp64 / Triton.dp32 + libz3.dll) in triton\build-x64 / triton\build-x32.
# Needs Visual Studio 2022 (C++), CMake 3.20+ and Ninja; downloads Z3 4.16.0 and Capstone 5.0.7 (pinned
# hashes) on the first configure.
#
#   powershell -File triton\build.ps1 [-Arch x64|x32|all] [-Sdk <x64dbg pluginsdk>]
param(
    [ValidateSet('x64', 'x32', 'all')]
    [string] $Arch = 'all',
    [string] $Sdk = ''
)
$ErrorActionPreference = 'Stop'

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'no Visual Studio with the C++ tools' }
$vcvarsall = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
$env:PATH = (Split-Path $vswhere) + ';' + $env:PATH

$sdkArg = if ($Sdk) { "-DX64DBG_SDK=`"$Sdk`"" } else { '' }
$targets = if ($Arch -eq 'all') { 'x64', 'x32' } else { , $Arch }
foreach ($target in $targets) {
    $vcarch = if ($target -eq 'x64') { 'x64' } else { 'x64_x86' }
    $build = Join-Path $PSScriptRoot "build-$target"
    $script = Join-Path ([IO.Path]::GetTempPath()) "fathom-triton-$target.cmd"
    Set-Content -Encoding ascii $script @(
        "@call `"$vcvarsall`" $vcarch >nul || exit /b 1",
        "@cmake -S `"$PSScriptRoot`" -B `"$build`" -G Ninja -DCMAKE_BUILD_TYPE=Release $sdkArg || exit /b 1",
        "@cmake --build `"$build`" --target triton_plugin triton_target || exit /b 1"
    )
    cmd /c "`"$script`""
    $code = $LASTEXITCODE
    Remove-Item $script -ErrorAction SilentlyContinue
    if ($code -ne 0) { throw "the $target build failed" }
    "built $build\Triton.dp$(if ($target -eq 'x64') { '64' } else { '32' })"
}
