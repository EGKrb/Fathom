# Builds NoVmp and vtildump twice: 64-bit (novmp\build\NoVmp.exe, vtildump.exe: VMProtect x64) and 32-bit
# (novmp\build32\NoVmp32.exe, vtildump32.exe: VMProtect x86). VTIL picks its architecture when it is compiled,
# hence two builds. Needs Visual Studio 2022 (C++), CMake 3.20+, Ninja and git (VTIL fetches Capstone and
# Keystone; linux-pe is downloaded at a pinned hash; the 32-bit build reuses the sources the 64-bit one fetched).
#
#   powershell -File novmp\build.ps1 [-Arch x64|x86|both]
param(
    [ValidateSet('x64', 'x86', 'both')] [string] $Arch = 'both'
)
$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'no Visual Studio with the C++ tools' }
$vcvarsall = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
$env:PATH = (Split-Path $vswhere) + ';' + $env:PATH

function Invoke-Build([string] $vcarch, [string] $dir, [string] $extra) {
    $script = Join-Path ([IO.Path]::GetTempPath()) "fathom-novmp-$vcarch.cmd"
    Set-Content -Encoding ascii $script @(
        "@call `"$vcvarsall`" $vcarch >nul || exit /b 1",
        "@cmake -S `"$PSScriptRoot`" -B `"$dir`" -G Ninja -DCMAKE_BUILD_TYPE=Release $extra || exit /b 1",
        "@cmake --build `"$dir`" --target NoVmp vtildump emulator_test || exit /b 1"
    )
    cmd /c "`"$script`""
    $code = $LASTEXITCODE
    Remove-Item $script -ErrorAction SilentlyContinue
    if ($code -ne 0) { throw "the NoVmp build failed ($vcarch)" }
}

$build = Join-Path $PSScriptRoot 'build'
if ($Arch -ne 'x86') {
    Invoke-Build 'x64' $build ''
    "built $build\NoVmp.exe, vtildump.exe"
}
if ($Arch -ne 'x64') {
    $build32 = Join-Path $PSScriptRoot 'build32'
    $deps = Join-Path $build '_deps'
    $extra = ''
    if (Test-Path (Join-Path $deps 'capstone-src')) {
        $extra = "-DFETCHCONTENT_SOURCE_DIR_CAPSTONE=`"$deps\capstone-src`" " +
                 "-DFETCHCONTENT_SOURCE_DIR_KEYSTONE=`"$deps\keystone-src`" " +
                 "-DFETCHCONTENT_SOURCE_DIR_LINUXPE=`"$deps\linuxpe-src`""
    }
    Invoke-Build 'x64_x86' $build32 $extra
    "built $build32\NoVmp32.exe, vtildump32.exe"
}
