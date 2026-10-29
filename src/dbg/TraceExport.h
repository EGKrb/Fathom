#ifndef TRACEEXPORT_H
#define TRACEEXPORT_H

#include "_global.h"

enum class TraceExportFormat
{
    Json,
    Csv,
    Tenet, // text trace of the Tenet IDA plugin (https://github.com/gaasedelen/tenet)
    Frinet, // Tenet text trace as written by the Frinet tracer (https://github.com/synacktiv/frinet)
};

struct TraceExportResult
{
    duint count = 0; // number of exported instructions
    bool truncated = false; // the last block was incomplete (trace still being recorded?)
    duint slide = 0; // Frinet: ASLR slide written to the file (0 = none)
    String error; // set when the export failed
};

// Convert a trace file (.trace32/.trace64) to a human-readable format.
// slide is only used by the Frinet format: 0 picks the main module base when the traced process is being debugged.
bool TraceExportFile(const char* traceFileName, const char* outputFileName, TraceExportFormat format, TraceExportResult & result, duint slide = 0);

#endif // TRACEEXPORT_H
