<#
.SYNOPSIS
Builds the minidump viewer (src\cross\minidump) for Windows x64.

.DESCRIPTION
Run it from an "x64 Native Tools" terminal (or after vcvars64.bat), after the 64-bit build
(build64), whose Qt 5.12.12 SDK is reused: the viewer then shares x64dbg's Qt DLLs.

The dependencies are cloned with git --ipv4 into build-cross-deps\ instead of being downloaded by
CMake (CMake downloads can fail on networks with broken IPv6, and GitHub sometimes answers 503 for
the json release archive).

Output: build-cross-x64\minidump.exe (copy it next to x64dbg.exe, in bin\x64 or release\x64).
#>
param(
    [string] $QtDir
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $QtDir) { $QtDir = Join-Path $root 'build64\_deps\qt5-src' }
if (-not (Test-Path (Join-Path $QtDir 'lib\cmake\Qt5'))) { throw "Qt not found in $QtDir (build the 64-bit x64dbg first, or pass -QtDir)" }
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) { throw 'cl.exe not found: run this script from an "x64 Native Tools" terminal' }

$deps = Join-Path $root 'build-cross-deps'
New-Item -ItemType Directory -Force $deps | Out-Null

# Same versions as src\cross\vendor\cmake.toml
$sources = [ordered]@{
    'json'            = @{ Url = 'https://github.com/nlohmann/json'; Ref = 'v3.12.0'; Shallow = $true }
    'cpp-httplib'     = @{ Url = 'https://github.com/yhirose/cpp-httplib'; Ref = 'v0.25.0' }
    'linux-pe'        = @{ Url = 'https://github.com/can1357/linux-pe'; Ref = '1fcb057963da3d25048bf1a331cd84aab9e09c99' }
    'PatternLanguage' = @{ Url = 'https://github.com/mrexodia/PatternLanguage'; Ref = 'werror-fixes' }
    'udmp-parser'     = @{ Url = 'https://github.com/0vercl0k/udmp-parser'; Ref = '2fff7ac40f22118cd7e68b1df71dbfd22a4906f6' }
}
foreach ($name in $sources.Keys) {
    $dir = Join-Path $deps $name
    if (Test-Path (Join-Path $dir '.git')) { continue }
    $source = $sources[$name]
    Write-Host "cloning $name ($($source.Ref))"
    if ($source.Shallow) {
        git clone -q --ipv4 --depth 1 --branch $source.Ref $source.Url $dir
    }
    else {
        git clone -q --ipv4 $source.Url $dir
        git -C $dir checkout -q $source.Ref
        git -C $dir submodule update -q --init --recursive
    }
    if ($LASTEXITCODE -ne 0) { throw "git failed for $name" }
}

$build = Join-Path $root 'build-cross-x64'
$d = $deps.Replace('\', '/')
$configure = @(
    '-S', (Join-Path $root 'src\cross'), '-B', $build, '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
    # cpp-httplib rejects the build when CMAKE_SYSTEM_VERSION is reported as 6.2.9200
    # (the version Windows hands a non-manifested process); pin it to the real host version.
    "-DCMAKE_SYSTEM_VERSION=$([Environment]::OSVersion.Version)",
    "-DCMAKE_PREFIX_PATH=$($QtDir.Replace('\', '/'))",
    '-DFETCHCONTENT_FULLY_DISCONNECTED=ON',
    "-DFETCHCONTENT_SOURCE_DIR_JSON=$d/json",
    "-DFETCHCONTENT_SOURCE_DIR_CPP-HTTPLIB=$d/cpp-httplib",
    "-DFETCHCONTENT_SOURCE_DIR_LINUX-PE=$d/linux-pe",
    "-DFETCHCONTENT_SOURCE_DIR_PATTERNLANGUAGE=$d/PatternLanguage",
    "-DFETCHCONTENT_SOURCE_DIR_UDMP-PARSER=$d/udmp-parser"
)
if (-not (Test-Path (Join-Path $build 'build.ninja'))) {
    cmake @configure
    if ($LASTEXITCODE -ne 0) { throw 'cmake configure failed' }
}
cmake --build $build --target minidump
if ($LASTEXITCODE -ne 0) { throw 'build failed' }
Write-Host "built $(Join-Path $build 'minidump.exe')"
