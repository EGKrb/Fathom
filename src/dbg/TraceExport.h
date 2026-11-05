#ifndef TRACEEXPORT_H
#define TRACEEXPORT_H

#include "_global.h"

enum class TraceExportFormat
{
    Json,
    Csv,
    Tenet, // text trace of the Tenet IDA plugin (https://github.com/gaasedelen/tenet)
    Frinet, // Tenet text trace as written by the Frinet tracer (https://github.com/synacktiv/frinet)
    Ftt, // Fathom Time Travel trace: binary, indexed, compressed (docs/developers/ftt.md)
};

struct TraceExportOptions
{
    duint slide = 0; // Frinet: ASLR slide (0 picks the main module base when the traced process is being debugged)
    uint32_t registers = 1; // ftt::RegisterLevel bits: the registers written by json, csv and ftt
    uint32_t thread = 0; // export only this thread's timeline (0 = all threads)
    uint32_t snapshotInterval = 65536; // ftt: instructions between two memory snapshots
};

struct TraceExportResult
{
    duint count = 0; // number of exported instructions
    bool truncated = false; // the last block was incomplete (trace still being recorded?)
    duint slide = 0; // Frinet: ASLR slide written to the file (0 = none)
    size_t threads = 0; // number of threads in the exported timeline
    size_t switches = 0; // context switches
    size_t samples = 0; // CPU time samples of the recorder (intervals whose concurrency is known)
    size_t concurrentSamples = 0; // intervals in which other threads ran outside the trace
    size_t snapshots = 0; // ftt: memory snapshots written
    size_t skippedCalls = 0; // calls that ran untraced (TraceSetSkip, user block 0x83)
    String error; // set when the export failed
};

// Convert a trace file (.trace32/.trace64 recorded by x64dbg, or a .ftt Fathom Time Travel trace) to
// a human-readable format, or a .trace32/.trace64 to the .ftt format.
bool TraceExportFile(const char* traceFileName, const char* outputFileName, TraceExportFormat format, TraceExportResult & result, const TraceExportOptions & options = TraceExportOptions());

#endif // TRACEEXPORT_H
