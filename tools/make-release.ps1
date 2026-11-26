<#
.SYNOPSIS
Builds the release archives of this fork from an existing build.

.DESCRIPTION
Prerequisites (see README.md, "Compiler"):
  - bin\x64 and bin\x32 built (cmake --build build64 / build32)
  - pybridge\build-x64\pybridge.dp64 and pybridge\build-x32\pybridge.dp32 built
  - extracmds\build-x64\ExtraCmds.dp64 and extracmds\build-x32\ExtraCmds.dp32 built
  - triton\build-x64\Triton.dp64 and triton\build-x32\Triton.dp32 built (triton\build.ps1)
  - decompiler\build-x64\Decompiler.dp64 and decompiler\build-x32\Decompiler.dp32 built (decompiler\build.ps1)
  - novmp\build\NoVmp.exe, vtildump.exe and novmp\build32\NoVmp32.exe, vtildump32.exe built (novmp\build.ps1)
  - remill\dist staged (remill\build.ps1)
  - build-cross-x64\minidump.exe built (tools\build-minidump-viewer.ps1)

Steps:
  1. downloads the x64dbg translations (like the official CI)
  2. runs cmake\release.cmake (official layout: release\, pluginsdk\, pdb\)
  3. adds ScyllaHide and xAnalyzer (official downloads, SHA-256 checked) and pybridge
  4. adds the Python client, examples, headless templates, HvKdFusion, the emulator, licenses and LISEZMOI.md
  5. writes dist\<Name>_<Version>.zip and dist\<Name>_<Version>_symbols.zip (Name defaults to Fathom), and
     dist\<Name>_<Version>_revng.zip when revng\dist is built (rev.ng, about 340 MB: its own archive, release\revng\)

OllyDumpEx is not redistributable: the archive contains install-plugins.ps1 to download it.

.EXAMPLE
.\tools\make-release.ps1 -Version 2026.10.03
#>
param(
    [string] $Version = (Get-Date -Format 'yyyy.MM.dd'),
    [string] $OutDir,
    [string] $ArchiveName = 'Fathom'   # archive base name: Fathom_<Version>.zip ($name is reused below)
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $OutDir) { $OutDir = Join-Path $root 'dist' }

function Invoke-Download([string] $Url, [string] $File) {
    & curl.exe -4 -fsSL --retry 3 -o $File $Url
    if ($LASTEXITCODE -ne 0) { throw "download failed: $Url" }
}

# 0. Checks
$required = @(
    'bin\x64\x64dbg.exe', 'bin\x64\headless.exe', 'bin\x32\x32dbg.exe', 'bin\x32\headless.exe', 'bin\Fathom.exe',
    'pybridge\build-x64\pybridge.dp64', 'pybridge\build-x32\pybridge.dp32',
    'extracmds\build-x64\ExtraCmds.dp64', 'extracmds\build-x32\ExtraCmds.dp32',
    'triton\build-x64\Triton.dp64', 'triton\build-x32\Triton.dp32',
    'decompiler\build-x64\Decompiler.dp64', 'decompiler\build-x32\Decompiler.dp32',
    'novmp\build\NoVmp.exe', 'novmp\build\vtildump.exe', 'novmp\build32\NoVmp32.exe', 'novmp\build32\vtildump32.exe',
    'remill\dist\remill-lift-17.exe', 'remill\dist\semantics\amd64_avx.bc', 'remill\dist\semantics\x86_avx.bc',
    'build-cross-x64\minidump.exe'
)
foreach ($file in $required) {
    if (-not (Test-Path (Join-Path $root $file))) { throw "missing $file, build it first" }
}
$commit = (git -C $root rev-parse HEAD).Trim()
if (git -C $root status --porcelain --untracked-files=no -- src pybridge/src triton/src decompiler/src extracmds/src) {
    Write-Warning "uncommitted changes in src or a plugin's src: the binaries may not match commit $commit"
}

# 1. Translations (same source as the official CI)
$qm = Join-Path $root 'bin\qm.zip'
Invoke-Download 'https://github.com/x64dbg/translations/releases/download/translations/qm.zip' $qm
Expand-Archive -Force $qm (Join-Path $root 'bin')
Remove-Item $qm

# 2. Official release layout
cmake -P (Join-Path $root 'cmake\release.cmake')
if ($LASTEXITCODE -ne 0) { throw 'cmake\release.cmake failed' }
$package = Join-Path $root 'release'
$main = Join-Path $package 'release'

# 3. Plugins + tools bundled in the release: ScyllaHide, xAnalyzer, PE-sieve.
# Not bundled (user runs install-plugins.ps1 on their own machine):
#   - OllyDumpEx: no redistribution license.
#   - TitanHide:  Defender/other AVs flag the kernel driver as HackTool; shipping the archive
#                 would break download/extraction for most users. install-plugins.ps1 can
#                 fetch it on demand once an exclusion is set up (see README-TitanHide.md).
& (Join-Path $root 'plugins\install-plugins.ps1') -X64dbgDir $main -Plugins ScyllaHide, xAnalyzer, 'PE-sieve' -Force
Copy-Item (Join-Path $root 'pybridge\build-x64\pybridge.dp64') (Join-Path $main 'x64\plugins')
Copy-Item (Join-Path $root 'pybridge\build-x32\pybridge.dp32') (Join-Path $main 'x32\plugins')
Copy-Item (Join-Path $root 'extracmds\build-x64\ExtraCmds.dp64') (Join-Path $main 'x64\plugins')
Copy-Item (Join-Path $root 'extracmds\build-x32\ExtraCmds.dp32') (Join-Path $main 'x32\plugins')
# Triton (symbolic execution, taint analysis: sym.* commands) and Z3, which it loads from its own folder
Copy-Item (Join-Path $root 'triton\build-x64\Triton.dp64'), (Join-Path $root 'triton\build-x64\libz3.dll') (Join-Path $main 'x64\plugins')
Copy-Item (Join-Path $root 'triton\build-x32\Triton.dp32'), (Join-Path $root 'triton\build-x32\libz3.dll') (Join-Path $main 'x32\plugins')
# Decompiler (Ghidra's decompiler: decompile command) and its sleigh specifications, in plugins\decompiler
foreach ($arch in 'x64', 'x32') {
    $ext = if ($arch -eq 'x64') { 'dp64' } else { 'dp32' }
    Copy-Item (Join-Path $root "decompiler\build-$arch\Decompiler.$ext") (Join-Path $main "$arch\plugins")
    Copy-Item -Recurse -Force (Join-Path $root "decompiler\build-$arch\decompiler") (Join-Path $main "$arch\plugins")
}
# Minidump viewer (tools\build-minidump-viewer.ps1): uses the Qt DLLs of x64dbg in x64\
Copy-Item (Join-Path $root 'build-cross-x64\minidump.exe') (Join-Path $main 'x64')
Copy-Item (Join-Path $root 'plugins\install-plugins.ps1') $main

# 4. pybridge client, examples, templates
$pybridge = Join-Path $main 'pybridge'
New-Item -ItemType Directory -Force $pybridge | Out-Null
foreach ($item in 'python', 'examples', 'headless', 'README.md') {
    Copy-Item -Recurse (Join-Path $root "pybridge\$item") $pybridge
}
Get-ChildItem -Recurse -Directory $pybridge -Filter '__pycache__' | Remove-Item -Recurse -Force

# HvKdFusion (used by the hvkd/wineye commands of ExtraCmds). config.json holds local paths: not shipped
$hvkd = Join-Path $main 'hvkdfusion'
New-Item -ItemType Directory -Force $hvkd | Out-Null
foreach ($item in 'hvkdfusion.py', 'wineye.py', 'hvkd.ps1', 'config.example.json', 'README.md', 'LICENSE', 'plugin_build') {
    Copy-Item -Recurse (Join-Path $root "hvkdfusion\$item") $hvkd
}

# Emulator (dumpulator, used by the emu.* commands of ExtraCmds and by pybridge's Debugger.emulator)
$emulator = Join-Path $main 'emulator'
New-Item -ItemType Directory -Force $emulator | Out-Null
foreach ($item in 'fathom_emu.py', 'requirements.txt', 'README.md', 'LICENSE.dumpulator', 'LICENSE.minidump', 'dumpulator', 'minidump', 'examples') {
    Copy-Item -Recurse (Join-Path $root "emulator\$item") $emulator
}
Get-ChildItem -Recurse -Directory $emulator -Filter '__pycache__' | Remove-Item -Recurse -Force
# the host of the child processes of the other architecture (dumped by the headless of that
# architecture when a child needs it): built with the tests
$hosts = Join-Path $emulator 'hosts'
New-Item -ItemType Directory -Force $hosts | Out-Null
foreach ($arch in 'x32', 'x64') {
    $hostExe = Join-Path $root "bin\$arch\tests\emulator_host.exe"
    if (-not (Test-Path $hostExe)) { throw "$hostExe is missing: build the tests (emulator_host)" }
    Copy-Item $hostExe (Join-Path $hosts "emulator_host_$arch.exe")
}

# NoVmp (novmp command of ExtraCmds: VMProtect devirtualization) and vtildump, x64 and x86 (NoVmp32, Fathom's port)
$novmp = Join-Path $main 'novmp'
New-Item -ItemType Directory -Force $novmp | Out-Null
Copy-Item (Join-Path $root 'novmp\build\NoVmp.exe'), (Join-Path $root 'novmp\build\vtildump.exe'), (Join-Path $root 'novmp\README.md') $novmp
Copy-Item (Join-Path $root 'novmp\build32\NoVmp32.exe'), (Join-Path $root 'novmp\build32\vtildump32.exe') $novmp

# remill (remill.lift command of ExtraCmds: machine code to LLVM IR), built for Windows, with its semantics
$remill = Join-Path $main 'remill'
New-Item -ItemType Directory -Force $remill | Out-Null
Copy-Item -Recurse (Join-Path $root 'remill\dist\*') $remill
Copy-Item (Join-Path $root 'remill\README.md') $remill

# Binary Ninja plugin (fathom_binja: synchronization with Fathom through pybridge), with pybridge's client next to it
$binja = Join-Path $main 'binja'
New-Item -ItemType Directory -Force $binja | Out-Null
Copy-Item -Recurse (Join-Path $root 'binja\fathom_binja') $binja
Copy-Item (Join-Path $root 'binja\README.md') $binja
Copy-Item (Join-Path $root 'pybridge\python\x64dbg_bridge.py') (Join-Path $binja 'fathom_binja')
Get-ChildItem -Recurse -Directory $binja -Filter '__pycache__' | Remove-Item -Recurse -Force

# angr / miasm (angr.* and miasm.* commands of ExtraCmds): scripts and miasm
$analysis = Join-Path $main 'analysis'
New-Item -ItemType Directory -Force $analysis | Out-Null
foreach ($item in 'fathom_angr.py', 'fathom_miasm.py', 'requirements.txt', 'README.md', 'LICENSE.miasm', 'miasm') {
    Copy-Item -Recurse (Join-Path $root "analysis\$item") $analysis
}
Get-ChildItem -Recurse -Directory $analysis -Filter '__pycache__' | Remove-Item -Recurse -Force

# Licenses
$licenses = Join-Path $main 'licenses'
New-Item -ItemType Directory -Force $licenses | Out-Null
Copy-Item (Join-Path $root 'LICENSE') (Join-Path $licenses 'x64dbg-LICENSE.txt')
Invoke-Download 'https://raw.githubusercontent.com/x64dbg/ScyllaHide/master/LICENSE' (Join-Path $licenses 'ScyllaHide-LICENSE.txt')
Invoke-Download 'https://raw.githubusercontent.com/ThunderCls/xAnalyzer/master/LICENSE' (Join-Path $licenses 'xAnalyzer-LICENSE.txt')
Copy-Item (Join-Path $root 'plugins\THIRD-PARTY.md') $licenses
Copy-Item (Join-Path $root 'emulator\LICENSE.dumpulator') (Join-Path $licenses 'dumpulator-LICENSE.txt')
Copy-Item (Join-Path $root 'emulator\LICENSE.minidump') (Join-Path $licenses 'dumpulator-minidump-LICENSE.txt')
# Triton plugin: Triton (Apache-2.0), Z3 (MIT) and Capstone (BSD), as built by triton\build.ps1
Copy-Item (Join-Path $root 'triton\third_party\Triton\LICENSE.txt') (Join-Path $licenses 'Triton-LICENSE.txt')
Copy-Item (Join-Path $root 'triton\build-x64\_deps\z3-src\LICENSE.txt') (Join-Path $licenses 'Z3-LICENSE.txt')
Copy-Item (Join-Path $root 'triton\build-x64\_deps\capstone-src\LICENSE.TXT') (Join-Path $licenses 'Capstone-LICENSE.txt')
# Decompiler plugin: Ghidra (Apache-2.0, with its NOTICE)
Copy-Item (Join-Path $root 'decompiler\third_party\ghidra\LICENSE') (Join-Path $licenses 'Ghidra-LICENSE.txt')
Copy-Item (Join-Path $root 'decompiler\third_party\ghidra\NOTICE') (Join-Path $licenses 'Ghidra-NOTICE.txt')
# NoVmp (GPL-3.0), VTIL (BSD-3-Clause), and what NoVmp.exe links: Keystone (GPL-2.0), Capstone, linux-pe (MIT)
Copy-Item (Join-Path $root 'novmp\third_party\NoVmp\LICENSE') (Join-Path $licenses 'NoVmp-LICENSE.txt')
Copy-Item (Join-Path $root 'novmp\third_party\VTIL-Core\LICENSE.md') (Join-Path $licenses 'VTIL-LICENSE.txt')
Copy-Item (Join-Path $root 'novmp\build\_deps\keystone-src\COPYING') (Join-Path $licenses 'Keystone-LICENSE.txt')
Copy-Item (Join-Path $root 'novmp\build\_deps\capstone-src\LICENSE.TXT') (Join-Path $licenses 'Capstone-NoVmp-LICENSE.txt')
Copy-Item (Join-Path $root 'novmp\build\_deps\linuxpe-src\LICENSE.md') (Join-Path $licenses 'linux-pe-LICENSE.txt') -ErrorAction SilentlyContinue
# remill (Apache-2.0) and what remill-lift links: LLVM (Apache-2.0 with LLVM exceptions), XED (Apache-2.0),
# glog, gflags (BSD-3-Clause), sleigh (Ghidra, Apache-2.0: Ghidra-LICENSE.txt above)
Copy-Item (Join-Path $root 'remill\third_party\remill\LICENSE') (Join-Path $licenses 'remill-LICENSE.txt')
$remillDeps = Join-Path $env:USERPROFILE 'remill-deps\build'
foreach ($dep in @{ 'LLVM' = 'llvm-prefix\src\llvm\llvm\LICENSE.TXT'; 'XED' = 'xed-prefix\src\xed\LICENSE';
                    'glog' = 'glog-prefix\src\glog\COPYING'; 'gflags' = 'gflags-prefix\src\gflags\COPYING.txt' }.GetEnumerator()) {
    $source = Join-Path $remillDeps $dep.Value
    if (Test-Path $source) { Copy-Item $source (Join-Path $licenses "remill-$($dep.Key)-LICENSE.txt") }
    else { Write-Warning "license of $($dep.Key) not found ($source)" }
}
# miasm (GPL-2.0-or-later)
Copy-Item (Join-Path $root 'analysis\LICENSE.miasm') (Join-Path $licenses 'miasm-LICENSE.txt')
# Libraries linked into minidump.exe
$viewerLicenses = @{ 'json' = 'LICENSE.MIT'; 'cpp-httplib' = 'LICENSE'; 'linux-pe' = 'LICENSE.md'; 'udmp-parser' = 'LICENSE' }
foreach ($name in $viewerLicenses.Keys) {
    Copy-Item (Join-Path $root "build-cross-deps\$name\$($viewerLicenses[$name])") (Join-Path $licenses "minidump-viewer_$name-LICENSE.txt")
}

# Quick start inside the archive
$readme = Get-Content -Raw (Join-Path $root 'tools\LISEZMOI.md')
$readme = $readme.Replace('%VERSION%', $Version).Replace('%COMMIT%', $commit)
[IO.File]::WriteAllText((Join-Path $main 'LISEZMOI.md'), $readme, (New-Object Text.UTF8Encoding $false))

# 5. Archives
New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path $OutDir "${ArchiveName}_$Version.zip"
$symbols = Join-Path $OutDir "${ArchiveName}_${Version}_symbols.zip"
$revngZip = Join-Path $OutDir "${ArchiveName}_${Version}_revng.zip"
Remove-Item -Force $zip, $symbols, $revngZip, (Join-Path $OutDir 'SHA256SUMS.txt') -ErrorAction SilentlyContinue
Compress-Archive -CompressionLevel Optimal -Path (Join-Path $package 'release'), (Join-Path $package 'pluginsdk'), (Join-Path $package 'commithash.txt') -DestinationPath $zip
Compress-Archive -CompressionLevel Optimal -Path (Join-Path $package 'pdb'), (Join-Path $package 'commithash.txt') -DestinationPath $symbols
$archives = @($zip, $symbols)

# rev.ng (revng.decompile command of ExtraCmds), built for Windows by revng\build.ps1: its own archive, release\revng\,
# extracted over the release
$revngDist = Join-Path $root 'revng\dist'
if (Test-Path (Join-Path $revngDist 'revng.cmd')) {
    $revngPackage = Join-Path $package 'revng-archive'
    $revngTarget = Join-Path $revngPackage 'release\revng'
    New-Item -ItemType Directory -Force $revngTarget | Out-Null
    Copy-Item -Recurse (Join-Path $revngDist '*') $revngTarget
    Copy-Item (Join-Path $root 'revng\README.md') $revngTarget
    $revngLicenses = Join-Path $revngTarget 'licenses'
    New-Item -ItemType Directory -Force $revngLicenses | Out-Null
    Copy-Item (Join-Path $root 'revng\third_party\revng\LICENSE') (Join-Path $revngLicenses 'revng-LICENSE.txt')
    Copy-Item (Join-Path $root 'revng\third_party\revng\LICENSE.mit') (Join-Path $revngLicenses 'revng-LICENSE.mit.txt')
    Copy-Item (Join-Path $root 'revng\third_party\FATHOM.md') (Join-Path $revngLicenses 'revng-FATHOM.md')
    Copy-Item (Join-Path $root 'revng\patches\qemu.patch') (Join-Path $revngLicenses 'qemu-changes.patch')
    Compress-Archive -CompressionLevel Optimal -Path (Join-Path $revngPackage 'release') -DestinationPath $revngZip
    $archives += $revngZip
} else {
    Write-Warning 'revng\dist not built (revng\build.ps1): no rev.ng archive'
}

$sums = ''
foreach ($file in $archives) {
    $hash = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant()
    $sums += "{0}  {1}`n" -f $hash, (Split-Path -Leaf $file)
    "{0,-45} {1,8:N1} MB  sha256 {2}" -f (Split-Path -Leaf $file), ((Get-Item $file).Length / 1MB), $hash
}
# LF line endings so that `sha256sum -c SHA256SUMS.txt` works
[IO.File]::WriteAllText((Join-Path $OutDir 'SHA256SUMS.txt'), $sums, (New-Object Text.ASCIIEncoding))
