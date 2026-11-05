# TraceSetThreads/SetTraceThreads

Choose the threads that the next traces single-step: only the current thread (default), or every thread of the debuggee.

## arguments

`[arg1]` `current` or `all`. Without argument, the current choice is printed.

## results

`$result` is set to 1, or to 0 when `all` is not supported by the debug engine: the TitanEngine emulation of GleeBug (`Engine` / `DebugEngine` = 1) cannot step every thread, the current thread is then traced.

## remarks

With `current`, the trace steps one thread while the others run freely: the order of the instructions of several threads is the order observed by the debugger, measured by the CPU time samples of the recorder (see [TraceExport](./TraceExport.md)).

With `all`, every thread of the debuggee is single-stepped, including the threads created during the trace. Each one reports each of its instructions; the recorder writes an instruction when its thread reports the next one, that is once it has executed. The trace therefore holds the instructions of all the threads in the order the debug events report them, and the memory written by an instruction is read after it executed, whatever the other threads did in between. The break condition, the log and the command of the trace are evaluated for every instruction of every thread: use `tid()` to restrict them to one thread.

Limits of `all`:

- The threads still run in parallel. Two threads that execute an instruction at the same time on two processors are ordered by their debug events, and the value read after the first instruction can already include the write of the second one. A memory value read by an instruction is the one when its thread stopped before it.
- A thread can lose its single step without reporting it: `NtContinue` (start of a thread, dispatch of an exception or an APC) and `NtSetContextThread` load a whole context. The engine checks the trap flag of the silent threads, more and more rarely while they stay blocked (every 1 to 64 events), and sets it again: a few instructions of such a thread can run untraced.
- Every thread runs at the speed of single-stepping: combine it with [TraceSetSkip](./TraceSetSkip.md) to run the calls into the system modules at full speed.
- A thread about to execute `PUSHF`, `POP SS` or `MOV SS` is armed after that instruction.

When the trace stops, the other threads stop being stepped. A thread that already executed its instruction has its single-step event queued: the engine drops that event when it arrives.

## example

```
var tid0, tid()
var sp0, csp
TraceSetThreads all
TraceSetSkip system
StartTraceRecording "trace.trace64"
TraceIntoConditional tid() == tid0 && csp > sp0, .1000000
StopTraceRecording
TraceSetThreads current
```
