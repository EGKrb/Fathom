# TraceExport

Convert a trace file recorded with [StartTraceRecording](./StartRunTrace.md) to a readable JSON or CSV file, or to the text trace format of the [Tenet](https://github.com/gaasedelen/tenet) and [Frinet](https://github.com/synacktiv/frinet) trace explorers. This command does not need an active debug session, so it can be used from a script in `headless.exe`.

## arguments

`arg1` The trace file to read (`.trace64` with x64dbg, `.trace32` with x32dbg).

`arg2` The output file. It is overwritten if it already exists.

`[arg3]` The output format: `json`, `csv`, `tenet` or `frinet`. When this argument is omitted, `csv` is used if the output file name ends with `.csv`, `frinet` if it ends with `.tenet` (the extension of the Frinet tracer), otherwise `json`.

`[arg4]` `frinet` only: the ASLR slide to write in the file, the runtime base of the traced module (for example `mod.main()`). When it is omitted, the base of the debugged module is used if the trace belongs to the process that is currently being debugged; otherwise no slide is written.

## result

`$result` will be set to the number of exported instructions.

## remarks

Every exported instruction contains its index, thread id, address, opcode bytes, disassembly, the general purpose registers *before* the instruction was executed and the memory it accessed (address, old value and new value, one pointer-sized value per access). The module name is included when the trace belongs to the process that is currently being debugged. All numbers except the index and thread id are hexadecimal strings, because 64-bit values do not fit in a JSON number.

In the CSV format, memory accesses are stored in the `memory` column as `address:old` for an unchanged value, or `address:old->new` for a modified value, separated by `;`.

The `tenet` and `frinet` formats write one line per instruction with comma-separated `name=0xvalue` entries: the general purpose registers that changed since the previous line (all of them on the first line), the instruction pointer, and the memory accesses as `mr=0xaddress:bytes` (unchanged value) or `mw=0xaddress:bytes` (modified value), where `bytes` is the content after execution in memory order. Flags are not exported because these tools do not read them. The two formats differ in what a line describes:

- `tenet` follows the reference tracers of Tenet: a line is the state *before* its instruction (registers first, instruction pointer last) with the memory accessed by the *previous* instruction. The memory accessed by the last instruction is not exported. The file loads in Tenet (IDA Pro, x86 and x64).
- `frinet` follows the Frinet tracer: an optional `slide=0x...` first line, then a line per instruction with the instruction pointer first, the registers *after* the instruction and the memory it accessed. The registers after the last instruction, or after an instruction followed by a thread switch, are unknown and not written. Index the file with `frinet index --format tenet trace.tenet trace.db`, then open the database in IDA Pro. This format is only available for 64-bit traces, because the Frinet IDA plugin only reads `rip` (x64) and `pc` (ARM64).

A trace that contains several threads is exported as a single timeline. x64dbg records each memory access as a pointer-sized value, so an access can cover more bytes than the instruction operand.

A trace that is still being recorded can be exported, the last incomplete instruction is skipped.

## example

```
StartTraceRecording "C:\traces\target.trace64"
TraceIntoConditional 0, .1000
StopTraceRecording
TraceExport "C:\traces\target.trace64", "C:\traces\target.json"
TraceExport "C:\traces\target.trace64", "C:\traces\target.csv"
TraceExport "C:\traces\target.trace64", "C:\traces\target.log", tenet
TraceExport "C:\traces\target.trace64", "C:\traces\target.tenet", frinet, mod.main()
```
