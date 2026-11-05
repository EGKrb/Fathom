# TraceSetSkip/SetTraceSkip

Run the calls into modules that are not traced at full speed during the next traces, instead of single-stepping every instruction of the called code. Most of the instructions executed by a program are often in the system modules (`printf`, the heap, `GetTickCount64`...): skipping them keeps the recording on the code being studied.

When a traced thread is about to execute a `call` whose destination is in a skipped module (through the import thunks `jmp [__imp_X]` and the `jmp` thunks of incremental linking), the call is stepped over: the called code runs without being stopped and the trace goes on at the return address. The call is recorded as one instruction, with the registers and memory after its return, as with [TraceOverConditional](./TraceOverConditional.md). Code that is outside any module (allocated or unpacked code) is always traced.

## arguments

`[arg1]` The mode. Without argument, the current mode is printed.

- `none` every instruction is traced (default).
- `system` skip the calls into the system modules (the modules of the Windows directory, the party shown in the Symbols view).
- `only` skip the calls into the modules that are not listed in the next arguments. Without module, only the debugged module is traced.

`[arg2...]` `only`: the modules to trace, by file name (`.dll` is added when the name has no extension).

## results

This command does not set any result variables. At the end of a trace, the log gives the number of calls that ran untraced.

## remarks

The trace records a skipped call with user block `0x83` (see [tracefile.md](../../developers/tracefile.md)); `TraceExport` adds a `skip` member (the destination) to the call in the JSON export.

The code that a skipped call runs is not recorded, including the callbacks into the traced code it makes (the comparison function of `qsort`, a window procedure...) and the memory it changes. An exception raised in the called code and handled by the traced code, or a call that does not return (`ExitThread`), ends the step over without coming back to the return address: the trace stops at the next event of the thread.

With [TraceSetThreads](./TraceSetThreads.md) `all`, the return address gets a breakpoint shared by the threads that wait for it, and a thread created during the trace runs its initialization by the loader at full speed until its start address when that address is in traced code.

## example

```
TraceSetSkip system
StartTraceRecording "trace.trace64"
TraceIntoConditional cip == ret_address, .1000000
StopTraceRecording
TraceSetSkip none
```
