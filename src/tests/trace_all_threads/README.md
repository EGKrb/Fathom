# trace_all_threads

Records `Work` with `TraceSetThreads all` and `TraceSetSkip system`: three workers increment a
shared counter with an interlocked instruction and call `GetTickCount64` between two increments,
while the main thread waits for them.

Every thread is single-stepped and each instruction is written when its thread reports the next
one, so the trace holds the instructions of all the threads in the order they completed. The calls
into the system modules (`CreateThread`, `WaitForMultipleObjects`, `GetTickCount64`) and the
initialization of the new threads by the loader run at full speed.

`check.py` reads the counter address and the worker ids written by the target, then checks in the
JSON export that every worker was recorded, that some calls ran untraced (`skip`) and that the
increments of the counter follow each other in the global order (0 -> 1, 1 -> 2, ...): this only
holds if the records are in execution order.

`test.current.txt` records the same function with `TraceSetSkip system` only (the current thread):
the workers are not recorded and the calls into the system modules are stepped over
(`check.current.py`).
