# HvKdFusion — Hyper-V kernel debugging and memory forensics

x64dbg debugs **user-mode processes**. HvKdFusion covers what it cannot reach: the **kernel and the whole memory of a Windows machine** — a Hyper-V virtual machine (live) or a memory image (kernel crash dump, raw dump). It is a Python front-end that ties together existing tools under one CLI and one `config.json`:

| Component | Role |
|---|---|
| **[LiveCloudKd](https://github.com/gerhart01/LiveCloudKd)** (`hvlib.dll` + `hvmm.sys`) | Hyper-V guest memory engine: list VMs, read/write physical and virtual guest memory, dump, pause/resume, read virtual-processor registers |
| **WinDbg / kd** | kernel debugger attached to the VM through `LiveCloudKd.exe` (live-kd or EXDI): full run control |
| **[MemProcFS](https://github.com/ufrisk/MemProcFS)** (`vmm.dll`) + **[LeechCore](https://github.com/ufrisk/LeechCore)** | memory analysis; LeechCore's `hvmm` device reads a live Hyper-V VM |
| **[Dokany](https://github.com/dokan-dev/dokany)** | file-system driver used by MemProcFS to mount memory as a drive (`M:`) |
| **[ntoseye](https://github.com/dmaivel/ntoseye)** | WinDbg-like offline analyzer (Linux/macOS, so WSL on Windows): REPL, MCP, DAP, GDB server |
| **wineye** (`wineye.py`, this folder) | native Windows, WinDbg-style REPL on passive memory, driving `vmm.dll` directly through ctypes: no WSL, no Dokany, no pip package |

Origin: [EGKrb/HvKdFusion](https://github.com/EGKrb/HvKdFusion), merged into this fork. None of the third-party components above is bundled: install or build them (see [Preparing the components](#preparing-the-components)), then run `setup`.

---

## From x64dbg

The **ExtraCmds** plugin adds these commands (command bar, scripts, `headless.exe`, pybridge):

| Command | Effect |
|---|---|
| `hvkd` | opens the HvKdFusion interactive menu in a console (pick a VM, then an action) |
| `hvkd action [options]` | runs any HvKdFusion action in a console, options as typed: `hvkd dump --vm 0 --out C:\dumps\vm0.raw` |
| `hvkd.log action [options]` | runs an action **without a window** and writes its output to the x64dbg log: `hvkd.log list`, `hvkd.log read --vm 0 --addr 0x1000 --size 0x100` |
| `hvkd.check [--deep]` | readiness table of every component, in the log |
| `wineye file` | opens a kernel crash dump or a raw memory image in wineye |
| `wineye --live --vm 0` | wineye on the live VM 0, with the freeze/resume/registers control channel; any wineye option is accepted |
| `.init MEMORY.DMP` / `.init file.raw` | a **kernel** crash dump (signature `PAGEDUMP`/`PAGEDU64`) or a `.raw`/`.mem`/`.vmem` image opens in wineye; a process minidump still opens in the minidump viewer |

- Memory operations on a VM need **administrator** rights: `hvkd` goes through `hvkd.ps1`, which asks for elevation (UAC) when the action needs it. `setup`, `config`, `check` and the analysis of an existing file (`wineye --dump`, `ntoseye --dump`) run without elevation. `hvkd.log` cannot show a UAC prompt: it only works for those actions, or when x64dbg itself runs as administrator.
- `hvkd.log` runs with standard input closed: an interactive action (`menu`, the wineye REPL) stops at its first prompt instead of blocking x64dbg. Use `hvkd` for those.
- Python 3.11+ must be on the `PATH` (`python.exe` or `py.exe`). The plugin looks for this folder at `release\hvkdfusion\` (release archive), then at `hvkdfusion\` at the root of the repository (development build in `bin\`).

---

## First run

```powershell
cd C:\Tools\x64dbg\release\hvkdfusion
python hvkdfusion.py setup        # locate + stage the components, write config.json
python hvkdfusion.py check        # what is ready, what is missing
.\hvkd.ps1                        # interactive menu, elevated
```

`setup` looks for the component source trees in `%USERPROFILE%\Downloads\<project>-master\...` by default (GitHub "Download ZIP" archives) and accepts an explicit path for each one: `--livecloudkd`, `--livecloudkd-exe`, `--windbg-dir` (an installed WinDbg), `--windbg-src`, `--memprocfs-exe`, `--memprocfs-src`, `--leechcore-src`, `--ntoseye-src`, `--install-ntoseye` (installs ntoseye in WSL if missing), `--no-extract`, `--no-stage`. It copies `hvlib.dll` + `hvmm.sys` next to `windbg.exe` and, when present, the `hvmm` LeechCore plugin next to `MemProcFS.exe`.

`check` reports each component (`[OK]` ready, `[~~]` optional missing, `[!!]` blocking missing) and which features are usable; `--deep` also runs `MemProcFS.exe -h`.

## Command line

```powershell
python hvkdfusion.py list                                     # Hyper-V VMs
python hvkdfusion.py read  --vm 0 --addr 0x10000 --size 0x1000
python hvkdfusion.py read  --vm 0 --addr 0xfffff80000000000 --size 0x200 --virtual
python hvkdfusion.py write --vm 0 --addr 0x10000 --data 9090cc --virtual
python hvkdfusion.py dump  --vm 0 --type linear --out guest.raw   # raw physical memory
python hvkdfusion.py dump  --vm 0 --type crash  --out guest.dmp   # Microsoft crash dump (LiveCloudKd.exe)
python hvkdfusion.py resume --vm 0

python hvkdfusion.py debug --vm 0 --engine windbg             # WinDbg on the VM (also: kd, exdi)

python hvkdfusion.py wineye --dump C:\dumps\vm0.raw
python hvkdfusion.py wineye --live --vm 0
python hvkdfusion.py wineye --dump vm0.raw -c "ps;lm" --batch --json

python hvkdfusion.py mount --dump C:\dumps\vm0.raw --letter M     # MemProcFS + Dokany
python hvkdfusion.py mount --from-vm 0 --forensic
python hvkdfusion.py mount --live                                 # live VM through the hvmm device
python hvkdfusion.py mount --device "PMEM://c:\temp\winpmem_x64.sys"

python hvkdfusion.py ntoseye --dump C:\dumps\MEMORY.DMP           # REPL (WSL)
python hvkdfusion.py ntoseye --vm 0                               # crash dump of VM 0, then ntoseye
python hvkdfusion.py ntoseye --dump MEMORY.DMP --mode mcp --http 127.0.0.1:8080
```

`hvkd.ps1 <action> ...` runs the same actions with automatic elevation.

Two modes:

- **engine** (`list`, `read`, `write`, `dump --type linear`, `resume`): only needs `hvlib.dll`, shipped prebuilt by LiveCloudKd;
- **launcher** (`debug`, `dump --type crash|raw`, `ntoseye --vm`): needs `LiveCloudKd.exe` (see below). Without it, actions fall back to the engine when they can.

`read_method` / `write_method` in `config.json` choose the memory access method: `0` = WinHv hypercall (slow, robust), `1` = raw `hvmm.sys` driver (fast, default). Symbols: `setx /m _NT_SYMBOL_PATH "SRV*C:\Symbols*https://msdl.microsoft.com/download/symbols"` (also kept in `config.json` as `symbol_path`).

## wineye

WinDbg-style commands on the memory of the target, which is not aware it is being inspected:

| Command | Effect |
|---|---|
| `ps` | processes |
| `!process [pid]` | process details (default: current context) |
| `.pid <n\|-1>` | process context (`-1` = physical memory) |
| `lm [pid]` | modules |
| `x <mod>!<sym>` | resolve a symbol through the PDB (`x nt!PsInitialSystemProcess`) |
| `db\|dw\|dd\|dq <addr> [L<n>]` | hex dump |
| `dt <mod>!<type> [field] [addr]` | type size, field offset (and value at `addr`) |
| `!vad [pid]`, `!handle [pid]` | VADs, handles |
| `s <addr> <len> <hex>` | byte pattern search |
| `info`, `help`, `q` | summary, help, quit |

Addresses accept `0x..`, decimal and `module!symbol`. On a **live** VM (`--live --vm N`, or `.ctl N` in the REPL), the hvlib control channel adds `freeze`, `resume` / `g` and `r [reg...]` (VP0 registers). There are no breakpoints or single-stepping there: hvlib delivers no trap events. For run control, use `hvkdfusion.py debug` (WinDbg/kd).

`wineye.py` can also be imported from Python (`Vmm` class) — e.g. next to a [pybridge](../pybridge/README.md) script, to compare what a process sees with what the kernel holds.

---

## Preparing the components

### Prerequisites

- Windows with Hyper-V, a running guest VM, **administrator** rights for memory operations;
- Visual C++ 2022 x64 runtime (`hvlib.dll`);
- Python 3.11+;
- 7-Zip or WinRAR on the `PATH` only to extract WinDbg from a `windbg64.part*.rar` source tree; with `--windbg-dir` (an installed WinDbg) nothing is extracted.

### LiveCloudKd.exe (launcher mode)

The `LiveCloudKd-master2` tree builds with toolset v143, SDK 10.0.26100, once its folders are laid out:

```powershell
$m2 = "$env:USERPROFILE\Downloads\LiveCloudKd-master2\LiveCloudKd-master"
Copy-Item "$m2\LiveCloudKdSdk\public" "$m2\public" -Recurse -Force
New-Item -ItemType Directory -Force "$m2\files", "$m2\LiveCloudKd\files" | Out-Null
Copy-Item "$m2\LiveCloudKdSdk\files\hvlib.lib" "$m2\files\" -Force
msbuild "$m2\LiveCloudKd\LiveCloudKd.sln" /p:Configuration=Release /p:Platform=x64 /p:SpectreMitigation=false
Copy-Item "$m2\LiveCloudKdSdk\files\hvlib.dll", "$m2\LiveCloudKdSdk\files\hvmm.sys" "$m2\LiveCloudKd\x64\Release\" -Force
python hvkdfusion.py setup --livecloudkd $m2 --livecloudkd-exe "$m2\LiveCloudKd\x64\Release\LiveCloudKd.exe"
```

`/p:SpectreMitigation=false` is only needed when the Spectre libraries are not installed. The executable requires elevation (`RequireAdministrator` manifest).

### MemProcFS and the hvmm plugin (wineye, mount, live device)

`MemProcFS.sln` expects LeechCore as a sibling folder (`..\LeechCore`); build `leechcore`, `vmm`, then `memprocfs` through the solution:

```powershell
$mp = "$env:USERPROFILE\Downloads\MemProcFS-master\MemProcFS-master"
$lc = "$env:USERPROFILE\Downloads\LeechCore-master\LeechCore-master"
New-Item -ItemType Junction -Path (Join-Path (Split-Path $mp) LeechCore) -Target $lc
foreach ($t in 'leechcore', 'vmm', 'memprocfs') {
    msbuild "$mp\MemProcFS.sln" /t:$t /p:Configuration=Release /p:Platform=x64 /p:SpectreMitigation=false
}
python hvkdfusion.py setup --memprocfs-exe "$mp\files\memprocfs.exe"
```

The `leechcore_device_hvmm.dll` plugin (live VM device) lives in the LiveCloudKd tree and needs `..\LeechCore`, the hvlib headers + `hvlib.lib`, and `charutil.c`. The headers and the library are in [`plugin_build/`](plugin_build), already laid out as the project expects (`files\hvlib.lib`, `hvlib\hvlib\include\public\`): unmodified copies of LiveCloudKd's `LiveCloudKdSdk\files\hvlib.lib` and `LiveCloudKdSdk\public\` (GPL-3.0).

```powershell
New-Item -ItemType Junction -Path "$m2\LeechCore" -Target $lc | Out-Null
$hk = "C:\Tools\x64dbg\release\hvkdfusion"                       # this folder (or hvkdfusion\ in a clone)
Copy-Item "$hk\plugin_build\*" $m2 -Recurse -Force                # files\, hvlib\hvlib\include\public\
# (without plugin_build: copy $m2\LiveCloudKdSdk\public\* to $m2\hvlib\hvlib\include\public\
#  and $m2\LiveCloudKdSdk\files\hvlib.lib to $m2\files\)
# add <ClCompile Include="..\LeechCore\leechcore\charutil.c" /> to the .vcxproj, then:
msbuild "$m2\leechcore_device_hvmm\leechcore_device_hvmm.vcxproj" /p:Configuration=Release /p:Platform=x64 /p:SolutionDir="$m2\" /p:SpectreMitigation=false
```

`setup --memprocfs-exe ...` then copies the plugin, `hvlib.dll` and `hvmm.sys` next to `MemProcFS.exe`.

### Dokany (mount only)

Install the signed `DokanSetup.exe` from <https://github.com/dokan-dev/dokany/releases> (kernel driver `dokan2.sys`). wineye does not need it.

### ntoseye (optional)

In WSL: `uv tool install ntoseye` (or `pipx` / `cargo`), or let `setup --install-ntoseye` do it. Linear dumps are raw, not `.dmp`: ntoseye needs a crash dump (`dump --type crash` or `ntoseye --vm N`). Windows paths are translated to `/mnt/c/...` automatically.

---

## Files

```
hvkdfusion.py        CLI: setup, check, list, read, write, dump, resume, debug, mount, ntoseye, wineye, menu
wineye.py            WinDbg-style REPL on vmm.dll (ctypes)
hvkd.ps1             launcher with automatic elevation (used by the x64dbg 'hvkd' command)
config.example.json  configuration keys; setup writes config.json (local paths, not versioned)
plugin_build/        LiveCloudKd hvlib headers + hvlib.lib, to build the hvmm LeechCore plugin
LICENSE              HvKdFusion license and third-party components
```

## Licenses

HvKdFusion: GPL-3.0-or-later (© 2026 EGKrb), see [LICENSE](LICENSE). `plugin_build/` holds unmodified LiveCloudKd files (GPL-3.0). The other third-party components, **not included**, keep their own licenses: LiveCloudKd/hvlib/hvmm binaries (GPL-3.0), MemProcFS and LeechCore (AGPL-3.0), Dokany (LGPL/MIT), ntoseye (its own license), WinDbg/kd (Microsoft license, not redistributed).
