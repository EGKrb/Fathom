# trace_export

Records a short trace of the target and converts it with the `TraceExport` command.

The script checks the exported instruction counts (JSON, CSV by extension, CSV by explicit
format argument, Tenet, and on x64 Frinet by extension and with an explicit slide). `check.py`
then parses `trace.json` and `trace.csv` and verifies that both files describe the same
instructions, and replays the Tenet and Frinet text traces to check that their registers and
memory accesses match the JSON export.
