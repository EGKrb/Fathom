# trace_export

Records a short trace of the target and converts it with the `TraceExport` command.

The script checks the exported instruction counts (JSON, CSV by extension, CSV by explicit
format argument, Tenet, and on x64 Frinet by extension and with an explicit slide). `check.py`
then parses `trace.json` and `trace.csv` and verifies that both files describe the same
instructions, and replays the Tenet and Frinet text traces to check that their registers and
memory accesses match the JSON export.

It also converts the trace to the `.ftt` format (by extension, and with every register and a
memory snapshot every 16 instructions), exports the `.ftt` back to JSON and Tenet, writes the SSE
registers to CSV and one thread's timeline to JSON. `check.py` reads the `.ftt` files with its own
parser (LZ4 included) and checks that they hold the instructions, registers, memory accesses and
context switches of the JSON export, that every memory snapshot matches the accesses replayed up to
it, that the JSON and Tenet exports of the `.ftt` are identical to those of the `.trace64`, and that
the extended registers and the single-thread timeline are right.
