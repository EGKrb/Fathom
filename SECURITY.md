# Security Policy

## Scope and intended use

**Fathom** is a Windows reverse-engineering and memory-forensics toolkit **built on [x64dbg](https://github.com/x64dbg/x64dbg)** (GPL-3.0). It is intended for **legitimate, authorized work**: malware analysis in a controlled environment, reverse-engineering of software you have the right to analyze, security research, CTF challenges, and debugging your own programs. Use it in accordance with the laws and agreements that apply to you.

Fathom is a debugging and analysis tool, not an exploitation framework. Please do not open issues asking for help attacking systems or software you are not authorized to analyze.

## A note on the debugging surface

Debuggers are powerful by design. A few Fathom-specific points worth knowing:

- **pybridge** opens a local control server. It listens on `127.0.0.1` only, starts on demand only, and accepts one client at a time. While it is running, **any local program that connects to the port can control the debugger** — read and write the debugged process's memory and run commands. Do not leave it running when you don't need it, and don't expose the port beyond localhost.
- **HvKdFusion** (`hvkd`, **wineye**) reads and writes the **memory of a whole machine** — a live Hyper-V VM or a memory image. Its memory operations require **administrator** rights, and it drives third-party components (LiveCloudKd, MemProcFS/LeechCore, WinDbg/kd, Dokany) that you install yourself. wineye's server modes (`--serve mcp|dap|gdb`) expose read-only memory over stdio or a local TCP port — keep those bound to localhost.
- The bundled plugins (**ScyllaHide**, **xAnalyzer**, optional **OllyDumpEx**) run with the debugger's privileges. Third-party downloads done by `install-plugins.ps1` are verified against the SHA-256 hashes in `plugins/THIRD-PARTY.md`; the install stops if a file does not match.

These are inherent properties of the tools, not vulnerabilities.

## Reporting a vulnerability

If you find a security issue **in Fathom's own code** (`TraceExport`, `pybridge`, `ExtraCmds`, `hvkdfusion/`, the release/packaging scripts) — for example memory corruption, a path-handling bug, or a server accepting input in an unsafe way:

1. **Do not** open a public issue describing the exploit.
2. Report it privately through [GitHub's "Report a vulnerability"](https://github.com/EGKrb/Fathom/security/advisories/new) (Security tab → Advisories), or by opening a minimal issue that only says you have a security report and asks for a contact channel.
3. Please include the affected version, a description, and reproduction steps.

Vulnerabilities in the **debugger core** or in a **bundled third-party component** should be reported to their respective upstream projects (x64dbg, ScyllaHide, xAnalyzer, OllyDumpEx, LiveCloudKd, MemProcFS, Dokany).

There is no formal SLA for this project, but reports are read and taken seriously.
