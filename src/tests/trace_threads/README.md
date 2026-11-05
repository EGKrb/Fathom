# trace_threads

Records a trace of the main thread while a worker thread spins (`busy`, `test.txt`) or waits on an
event (`wait`, `test.wait.txt`), then converts it with `TraceExport`.

The recorder writes a CPU time sample of every thread (`QueryThreadCycleTime` and a hash of the
user-mode registers, user block `0x82`) every 4096 instructions. The script checks the number of
sampled intervals (`$result1`) and the intervals in which another thread executed code outside the
trace (`$result2`): some with the spinning worker, none with the blocked one, also after a
conversion to `.ftt` and back. A timeline filtered on one thread has no samples.

`check.py` (`check.wait.py` for the blocked worker) reads the worker id written by the target and
checks that the intervals follow each other, that the recorded thread is never listed as running
outside the trace and that the samples of the `.ftt` are those of the `.trace64`. With the spinning
worker, it executed code outside the trace in most intervals; with the blocked one, no thread did:
the threads of the loader pool only use CPU time in the kernel, woken by the suspensions of the
debugger.
