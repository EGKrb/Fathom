# Binary Ninja and Fathom

Two ways to work with [Binary Ninja](https://binary.ninja) next to the debugger.

## The `binja` command (every edition, Free included)

```
binja                      // the module of the selection (else cip)
binja mymodule.decrypt     // the module that contains this address, opened at it
```

ExtraCmds reads the module **from memory** (unpacked or patched as it is now, at its load address), writes it as a PE file (`%TEMP%\Fathom\binja\<module>_<base>\`) and opens it in Binary Ninja at the address, through Binary Ninja's `binaryninja:` URL (Binary Ninja asks to confirm). Since the image is at its load address, the addresses are those of the debuggee. Binary Ninja is looked for in `%LOCALAPPDATA%\Programs\Vector35\BinaryNinja` (per-user installation) and `%ProgramFiles%\Vector35\BinaryNinja`; `FATHOM_BINARYNINJA` gives another `binaryninja.exe` (or its folder). `$result` = the process ID of Binary Ninja.

## The `fathom_binja` plugin (paid editions: Personal, Commercial...)

The Free edition of Binary Ninja runs no plugins and has no API; with another edition, the plugin synchronizes Binary Ninja with Fathom through [pybridge](../pybridge/README.md). In Fathom, `pybridge.start` (pybridge listens on 127.0.0.1:27041); in Binary Ninja, menu **Plugins > Fathom**:

| Command | |
| --- | --- |
| Connect | finds the module of the view in the debuggee (same file name) and maps the view's addresses to the debuggee's (the module may be loaded elsewhere) |
| Follow the debugger | the view follows the instruction pointer of the debuggee, at each stop (again: stops following) |
| Show in Fathom | Fathom's disassembly goes to the selected address |
| Set / Delete breakpoint | at the selected address |
| Run to here | the debuggee runs to the selected address |
| Import a trace | a trace exported by Fathom (`TraceExport`, json): the instructions executed are highlighted, with a tag giving how many times |

**Installation**: copy `fathom_binja` into `%APPDATA%\Binary Ninja\plugins`. It needs pybridge's Python client, `x64dbg_bridge.py`: in a Fathom release it is already in `fathom_binja`; from the repository, copy `pybridge\python\x64dbg_bridge.py` there, or set `FATHOM_PYBRIDGE` to its folder. pybridge serves one client at a time.

## Test

`binja/tests/test_binja.py`: the plugin's synchronization (`sync.py`: address mapping, breakpoints, run to here, following, traces) against a real Fathom (headless, pybridge) with a stand-in for the `binaryninja` module; `extracmds/tests/test_tools.py` runs the `binja` command with a stand-in for Binary Ninja that receives the URL.
