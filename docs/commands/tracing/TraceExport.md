# TraceExport

Convert a trace file recorded with [StartTraceRecording](./StartRunTrace.md) to a readable JSON or CSV file, to the text trace format of the [Tenet](https://github.com/gaasedelen/tenet) and [Frinet](https://github.com/synacktiv/frinet) trace explorers, or to the Fathom Time Travel format (`.ftt`: binary, indexed, compressed, with per-thread timelines and memory snapshots, see [ftt.md](../../developers/ftt.md)) that the **Time Travel** tab navigates backward and forward. A `.ftt` file can also be the input, to export it again to the other formats. This command does not need an active debug session, so it can be used from a script in `headless.exe`.

## arguments

`arg1` The trace file to read (`.trace64` with x64dbg, `.trace32` with x32dbg, or a `.ftt` of the same architecture).

`arg2` The output file. It is overwritten if it already exists.

`[arg3]` The output format: `json`, `csv`, `tenet`, `frinet` or `ftt`. When this argument is omitted, `csv` is used if the output file name ends with `.csv`, `frinet` if it ends with `.tenet` (the extension of the Frinet tracer), `ftt` if it ends with `.ftt`, otherwise `json`.

`[arg4]` `frinet` only: the ASLR slide to write in the file, the runtime base of the traced module (for example `mod.main()`). When it is omitted, the base of the debugged module is used if the trace belongs to the process that is currently being debugged; otherwise no slide is written.

`[options]` Any further argument is a `key=value` option:

- `regs=` the registers written by `json`, `csv` and `ftt`: `gpr` (general purpose registers, instruction pointer and flags, the default), `fpu` (+ x87: `st0`-`st7`, `fpcw`, `fpsw`, `fptw`), `sse` (+ `xmm` registers and `mxcsr`), `avx` (+ SSE and the upper halves `ymm0h`-...), `all`, or a combination such as `fpu+sse`. Tenet and Frinet only read the general purpose registers. From a `.ftt` source, only the registers it holds can be exported.
- `thread=` export only the timeline of this thread (thread id). With `frinet`, every line then has the registers after its instruction.
- `snapshot=` `ftt` only: instructions between two memory snapshots (default `.65536`). Smaller: larger file, faster random access.
- `slide=` the `frinet` slide, as `arg4`.

## result

`$result` will be set to the number of exported instructions. The log also gives the number of threads and context switches (and of memory snapshots for `ftt`).

`$result1` is set to the number of intervals between CPU time samples of the recorder, and `$result2` to the number of those intervals in which another thread executed code outside the trace (approximate order); the others have an exact order. Both are 0 with `thread=`, and for traces recorded before the samples existed.

## remarks

Every exported instruction contains its index, its global sequence number (`seq`: its position among the instructions of all threads, equal to the index unless `thread=` is used), thread id, address, opcode bytes, disassembly, the registers *before* the instruction was executed and the memory it accessed (address, size, old value and new value, one pointer-sized value per access in a `.trace64`). In JSON, an instruction that follows an instruction of another thread has a `switch` member: `{"from": thread, "timestamp": time}` (100 ns units, when the recorder wrote a context switch marker). An instruction that follows a CPU time sample of the recorder has a `cpu` member: `{"from": seq, "interval": duration, "outside": {thread: cycles}, "kernel": {thread: cycles}}` for the interval of instructions `[from, seq)` (duration in 100 ns units): `outside` lists the other threads that executed code outside the trace (the order is exact when it is empty), `kernel` those that only used CPU time in the kernel. The module name is included when the trace belongs to the process that is currently being debugged. All numbers except the index and thread id are hexadecimal strings, because 64-bit values do not fit in a JSON number.

A call that ran untraced ([TraceSetSkip](./TraceSetSkip.md)) has a `skip` member in JSON: the called address; the next instruction of its thread is at the return address. The log gives the number of such calls.

In the CSV format, memory accesses are stored in the `memory` column as `address:old` for an unchanged value, or `address:old->new` for a modified value, separated by `;`. The registers are the columns before it, the `seq` column is the last one.

The `tenet` and `frinet` formats write one line per instruction with comma-separated `name=0xvalue` entries: the general purpose registers that changed since the previous line (all of them on the first line), the instruction pointer, and the memory accesses as `mr=0xaddress:bytes` (unchanged value) or `mw=0xaddress:bytes` (modified value), where `bytes` is the content after execution in memory order. Flags are not exported because these tools do not read them. The two formats differ in what a line describes:

- `tenet` follows the reference tracers of Tenet: a line is the state *before* its instruction (registers first, instruction pointer last) with the memory accessed by the *previous* instruction. The memory accessed by the last instruction is not exported. The file loads in Tenet (IDA Pro, x86 and x64).
- `frinet` follows the Frinet tracer: an optional `slide=0x...` first line, then a line per instruction with the instruction pointer first, the registers *after* the instruction and the memory it accessed. The registers after the last instruction, or after an instruction followed by a thread switch, are unknown and not written. Index the file with `frinet index --format tenet trace.tenet trace.db`, then open the database in IDA Pro. This format is only available for 64-bit traces, because the Frinet IDA plugin only reads `rip` (x64) and `pc` (ARM64).

A trace that contains several threads is exported as a single timeline in `json`, `csv`, `tenet` and `frinet`, unless `thread=` selects one; the `ftt` format keeps one synchronized timeline per thread. x64dbg records each memory access as a pointer-sized value, so an access can cover more bytes than the instruction operand.

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
TraceExport "C:\traces\target.trace64", "C:\traces\target.ftt", ftt, regs=all
TraceExport "C:\traces\target.ftt", "C:\traces\main-thread.json", json, regs=sse, thread=tid()
```
