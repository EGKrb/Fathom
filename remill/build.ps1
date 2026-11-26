# Builds remill for Windows and stages it in remill\dist (remill-lift-17.exe, semantics\*.bc). Needs Visual
# Studio 2022 (C++), CMake 3.21+, Ninja, git and Python. The dependency superbuild (LLVM 17 with clang, XED,
# glog, gflags, googletest, sleigh) takes hours the first time; it goes to a short path ($Deps) because LLVM's
# object paths exceed 260 characters under the repository.
#
#   powershell -File remill\build.ps1 [-Deps C:\Users\<you>\remill-deps]
param(
    [string] $Deps = (Join-Path $env:USERPROFILE 'remill-deps')
)
$ErrorActionPreference = 'Stop'

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'no Visual Studio with the C++ tools' }
$vcvarsall = Join-Path $vs 'VC\Auxiliary\Build\vcvarsall.bat'
$env:PATH = (Split-Path $vswhere) + ';' + $env:PATH

$source = Join-Path $PSScriptRoot 'third_party\remill'
$install = Join-Path $Deps 'install'
$clangcl = Join-Path $install 'bin\clang-cl.exe'
$build = Join-Path $Deps 'remill-build'
$script = Join-Path ([IO.Path]::GetTempPath()) 'fathom-remill.cmd'
Set-Content -Encoding ascii $script @(
    "@call `"$vcvarsall`" x64 >nul || exit /b 1",
    # 1. the dependencies (MSVC)
    "@cmake -S `"$source\dependencies`" -B `"$Deps\build`" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=`"$install`" -DENABLE_SLEIGH=ON || exit /b 1",
    "@cmake --build `"$Deps\build`" || exit /b 1",
    # 2. remill (clang-cl of that LLVM)
    "@cmake -S `"$source`" -B `"$build`" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=`"$install`" -DREMILL_ENABLE_TESTING=OFF -DREMILL_FETCH_SLEIGH=OFF -DCMAKE_C_COMPILER=`"$clangcl`" -DCMAKE_CXX_COMPILER=`"$clangcl`" `"-DCMAKE_CXX_FLAGS=-D_ALLOW_COMPILER_AND_STL_VERSION_MISMATCH /EHsc`" || exit /b 1",
    "@cmake --build `"$build`" || exit /b 1"
)
cmd /c "`"$script`""
$code = $LASTEXITCODE
Remove-Item $script -ErrorAction SilentlyContinue
if ($code -ne 0) { throw 'the remill build failed' }

# 3. remill\dist: the executable, the x86 / x64 semantics next to it, the DLLs it imports from outside Windows
$dist = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Force (Join-Path $dist 'semantics') | Out-Null
Copy-Item (Join-Path $build 'bin\lift\remill-lift-17.exe') $dist
foreach ($arch in 'amd64', 'amd64_avx', 'amd64_avx512', 'x86', 'x86_avx', 'x86_avx512') {
    Copy-Item (Join-Path $build "lib\Arch\X86\Runtime\$arch.bc") (Join-Path $dist 'semantics')
}
# a zlib DLL, if an older LLVM build of $Deps found one (third_party/remill/dependencies/llvm.cmake disables zlib now)
foreach ($dll in 'zlib1__.dll', 'zlib1.dll', 'zlib.dll') {
    $found = Get-Command $dll -ErrorAction SilentlyContinue
    if ($found -and (Select-String -Quiet -SimpleMatch $dll (Join-Path $dist 'remill-lift-17.exe'))) {
        Copy-Item $found.Source $dist
    }
}
"remill staged in $dist"
