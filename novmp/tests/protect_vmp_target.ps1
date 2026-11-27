# Builds novmp/tests/vmp_target.c (x64 and x86) and protects it with VMProtect, to test NoVmp against VMProtect
# itself. VMProtect is commercial; its official demo (signed, from https://www.vmpsoft.com/download) is enough:
# the demo virtualizes with the real VM, only showing a nag and limiting some features. Nothing here is committed
# (a VMProtect-protected binary embeds VMProtect's runtime): this script just reproduces the test binaries.
#
#   powershell -File novmp\tests\protect_vmp_target.ps1 [-VMProtect "C:\Program Files\VMProtect Demo"] [-Out C:\...\vmp-test]
param(
    [string] $VMProtect = 'C:\Program Files\VMProtect Demo',
    [string] $Out = (Join-Path $env:USERPROFILE 'vmp-test')
)
$ErrorActionPreference = 'Stop'
$con = Join-Path $VMProtect 'VMProtect_Con.exe'
if (-not (Test-Path $con)) { throw "VMProtect_Con.exe not found in $VMProtect (install the demo from vmpsoft.com)" }

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvarsall = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
$src = Join-Path $PSScriptRoot 'vmp_target.c'
$inc = Join-Path $VMProtect 'Include\C'
$lib = Join-Path $VMProtect 'Lib\Windows'
New-Item -ItemType Directory -Force $Out | Out-Null

foreach ($arch in 'x64', 'x86') {
    $vcarch = if ($arch -eq 'x64') { 'x64' } else { 'x64_x86' }
    $exe = Join-Path $Out "vmp_target_$arch.exe"
    $vmp = Join-Path $Out "vmp_target_$arch.vmp.exe"
    $script = Join-Path ([IO.Path]::GetTempPath()) "fathom-vmp-$arch.cmd"
    Set-Content -Encoding ascii $script @(
        "@call `"$vcvarsall`" $vcarch >nul || exit /b 1",
        "@cl /nologo /O1 /GS- /I`"$inc`" `"$src`" /Fe:`"$exe`" /link /LIBPATH:`"$lib`" || exit /b 1"
    )
    cmd /c "`"$script`""
    Remove-Item $script -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) { throw "compilation failed ($arch)" }
    & $con $exe $vmp
    if ($LASTEXITCODE -ne 0) { throw "VMProtect failed ($arch)" }
    "protected $vmp"
}
"`nRun NoVmp on an UNPACKED image (NoVmp needs the module unpacked, as Fathom's novmp reads it from memory):"
"  novmp\build\NoVmp.exe   <unpacked x64>   (VMProtect 3.0 - 3.5)"
"  novmp\build32\NoVmp32.exe <unpacked x86> (set NOVMP_DEBUG_VMENTER=1 to dump a VM entry)"
