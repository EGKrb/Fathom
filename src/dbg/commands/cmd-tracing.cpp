#include "cmd-tracing.h"
#include "debugger.h"
#include "threading.h"
#include "module.h"
#include "console.h"
#include "cmd-debug-control.h"
#include "value.h"
#include "variable.h"
#include "TraceRecord.h"
#include "TraceExport.h"
#include "ftt/ftt.h"

extern std::vector<std::pair<duint, duint>> RunToUserCodeBreakpoints;

static bool genericConditionalTraceCommand(TITANCBSTEP callback, STEPFUNCTION stepFunction, int argc, char* argv[])
{
    if(IsArgumentsLessThan(argc, 2))
        return false;
    if(dbgtraceactive())
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "Trace already active"));
        return false;
    }
    if(dbgisrunning())
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "Cannot start a trace when running, pause execution first."));
        return false;
    }
    duint maxCount;
    if(!BridgeSettingGetUint("Engine", "MaxTraceCount", &maxCount) || !maxCount)
        maxCount = 50000;
    if(argc > 2 && !valfromstring(argv[2], &maxCount, false))
        return false;
    if(!dbgsettracecondition(*argv[1] ? argv[1] : "0", maxCount))
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), argv[1]);
        return false;
    }

    stepFunction(callback);
    dbgtracearmthreads(callback);
    return cbDebugRunInternal(1, argv, history_clear);
}

static bool conditionalTraceIntoCommand(TITANCBSTEP callback, int argc, char* argv[])
{
    // Select step function based on party filter
    STEPFUNCTION stepFunction;
    auto party = dbggettracepartyfilter();
    if(party == mod_user)
        stepFunction = StepIntoUser;
    else if(party == mod_system)
        stepFunction = StepIntoSystem;
    else if(party == -1)
        stepFunction = StepIntoWow64;
    else
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Unsupported party filter: %d\n"), party);
        return false;
    }
    return genericConditionalTraceCommand(callback, stepFunction, argc, argv);
}

static bool conditionalTraceOverCommand(TITANCBSTEP callback, int argc, char* argv[])
{
    // Select step function based on party filter
    STEPFUNCTION stepFunction;
    auto party = dbggettracepartyfilter();
    if(party == mod_user)
        stepFunction = StepOverUser;
    else if(party == mod_system)
        stepFunction = StepOverSystem;
    else if(party == -1)
        stepFunction = StepOverWrapper;
    else
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Unsupported party filter: %d\n"), party);
        return false;
    }
    return genericConditionalTraceCommand(callback, stepFunction, argc, argv);
}

bool cbDebugTraceIntoConditional(int argc, char* argv[])
{
    return conditionalTraceIntoCommand(cbTraceIntoConditionalStep, argc, argv);
}

bool cbDebugTraceOverConditional(int argc, char* argv[])
{
    return conditionalTraceOverCommand(cbTraceOverConditionalStep, argc, argv);
}

bool cbDebugTraceIntoBeyondTraceRecord(int argc, char* argv[])
{
    if(argc == 1)
    {
        const char* new_argv[] = { "tibt", "0" };
        return conditionalTraceIntoCommand(cbTraceIntoBeyondTraceRecordStep, 2, (char**)new_argv);
    }
    else
        return conditionalTraceIntoCommand(cbTraceIntoBeyondTraceRecordStep, argc, argv);
}

bool cbDebugTraceOverBeyondTraceRecord(int argc, char* argv[])
{
    if(argc == 1)
    {
        const char* new_argv[] = { "tobt", "0" };
        return conditionalTraceOverCommand(cbTraceOverBeyondTraceRecordStep, 2, (char**)new_argv);
    }
    else
        return conditionalTraceOverCommand(cbTraceOverBeyondTraceRecordStep, argc, argv);
}

bool cbDebugTraceIntoIntoTraceRecord(int argc, char* argv[])
{
    if(argc == 1)
    {
        const char* new_argv[] = { "tiit", "0" };
        return conditionalTraceIntoCommand(cbTraceIntoIntoTraceRecordStep, 2, (char**)new_argv);
    }
    else
        return conditionalTraceIntoCommand(cbTraceIntoIntoTraceRecordStep, argc, argv);
}

bool cbDebugTraceOverIntoTraceRecord(int argc, char* argv[])
{
    if(argc == 1)
    {
        const char* new_argv[] = { "toit", "0" };
        return conditionalTraceOverCommand(cbTraceOverIntoTraceRecordStep, 2, (char**)new_argv);
    }
    else
        return conditionalTraceOverCommand(cbTraceOverIntoTraceRecordStep, argc, argv);
}

bool cbDebugRunToParty(int argc, char* argv[])
{
    if(dbgisrunning())
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "Cannot start a trace when running, pause execution first."));
        return false;
    }
    EXCLUSIVE_ACQUIRE(LockRunToUserCode);
    if(!RunToUserCodeBreakpoints.empty())
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "Run to party is busy.\n"));
        return false;
    }
    if(IsArgumentsLessThan(argc, 2))
        return false;
    int party = atoi(argv[1]); // party is a signed integer
    ModEnum([party](const MODINFO & i)
    {
        if(i.party == party)
        {
            for(auto j : i.sections)
            {
                BREAKPOINT bp;
                if(!BpGet(j.addr, BPMEMORY, nullptr, &bp))
                {
                    size_t size = DbgMemGetPageSize(j.addr);
                    RunToUserCodeBreakpoints.emplace_back(j.addr, size);
                    SetMemoryBPXEx(j.addr, size, UE_MEMORY_EXECUTE, false, cbRunToUserCodeBreakpoint);
                }
            }
        }
    });
    return cbDebugRunInternal(1, argv, history_clear);
}

bool cbDebugRunToUserCode(int argc, char* argv[])
{
    const char* newargv[] = { "RunToParty", "0" };
    return cbDebugRunToParty(2, (char**)newargv);
}

bool cbDebugTraceSetLog(int argc, char* argv[])
{
    auto text = argc > 1 ? argv[1] : "";
    auto condition = argc > 2 ? argv[2] : "";
    if(!dbgsettracelog(condition, text))
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), condition);
        return false;
    }
    return true;
}

bool cbDebugTraceSetCommand(int argc, char* argv[])
{
    auto text = argc > 1 ? argv[1] : "";
    auto condition = argc > 2 ? argv[2] : "";
    if(!dbgsettracecmd(condition, text))
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), condition);
        return false;
    }
    return true;
}

bool cbDebugTraceSetLogFile(int argc, char* argv[])
{
    auto fileName = argc > 1 ? argv[1] : "";
    return dbgsettracelogfile(fileName);
}

bool cbDebugTraceSetStepFilter(int argc, char* argv[])
{
    if(IsArgumentsLessThan(argc, 2))
        return false;

    auto filter = argv[1];
    if(_stricmp(filter, "none") == 0)
    {
        dbgsettracepartyfilter(-1);
        dputs(QT_TRANSLATE_NOOP("DBG", "Step filter set to: none"));
    }
    else if(_stricmp(filter, "user") == 0)
    {
        dbgsettracepartyfilter(mod_user);
        dputs(QT_TRANSLATE_NOOP("DBG", "Step filter set to: user"));
    }
    else if(_stricmp(filter, "system") == 0)
    {
        dbgsettracepartyfilter(mod_system);
        dputs(QT_TRANSLATE_NOOP("DBG", "Step filter set to: system"));
    }
    else
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid step filter \"%s\", valid options are: none, user, system\n"), filter);
        return false;
    }
    return true;
}

// TraceSetSkip none|system|only[, module...]: the calls into the modules that are not traced run at
// full speed and are recorded as one instruction
bool cbDebugTraceSetSkip(int argc, char* argv[])
{
    std::vector<String> modules;
    if(argc < 2)
    {
        auto mode = dbggettraceskip(&modules);
        if(mode == TRACE_SKIP_SYSTEM)
            dputs(QT_TRANSLATE_NOOP("DBG", "Trace skip: calls into the system modules"));
        else if(mode == TRACE_SKIP_ONLY)
        {
            String list;
            for(auto & module : modules)
                list += (list.empty() ? "" : ", ") + module;
            dprintf(QT_TRANSLATE_NOOP("DBG", "Trace skip: calls out of %s\n"), list.empty() ? "the debuggee" : list.c_str());
        }
        else
            dputs(QT_TRANSLATE_NOOP("DBG", "Trace skip: none"));
        return true;
    }
    int mode;
    if(_stricmp(argv[1], "none") == 0)
        mode = TRACE_SKIP_NONE;
    else if(_stricmp(argv[1], "system") == 0)
        mode = TRACE_SKIP_SYSTEM;
    else if(_stricmp(argv[1], "only") == 0)
        mode = TRACE_SKIP_ONLY;
    else
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid trace skip \"%s\", valid options are: none, system, only\n"), argv[1]);
        return false;
    }
    for(int i = 2; i < argc; i++)
    {
        if(mode != TRACE_SKIP_ONLY)
        {
            dputs(QT_TRANSLATE_NOOP("DBG", "Only \"TraceSetSkip only\" takes a list of modules"));
            return false;
        }
        String module = argv[i];
        if(module.find('.') == String::npos)
            module += ".dll";
        modules.push_back(module);
    }
    if(!dbgsettraceskip(mode, modules))
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "Cannot change the trace skip while a trace is active"));
        return false;
    }
    return cbDebugTraceSetSkip(1, argv);
}

// TraceSetThreads all|current: trace every thread (each instruction in the order the threads execute
// them) or only the current thread (default)
bool cbDebugTraceSetThreads(int argc, char* argv[])
{
    if(argc > 1)
    {
        bool all;
        if(_stricmp(argv[1], "all") == 0)
            all = true;
        else if(_stricmp(argv[1], "current") == 0)
            all = false;
        else
        {
            dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid trace threads \"%s\", valid options are: all, current\n"), argv[1]);
            return false;
        }
        // $result: 1 when every thread can be traced, so that a script can do without it
        varset("$result", duint(1), false);
        if(all && !dbgtracethreadssupported())
        {
            varset("$result", duint(0), false);
            dputs(QT_TRANSLATE_NOOP("DBG", "TraceSetThreads all is not supported by this debug engine (it needs the TitanEngine of Fathom), the current thread is traced"));
            return true;
        }
        if(!dbgsettracethreads(all))
        {
            dputs(QT_TRANSLATE_NOOP("DBG", "Cannot change the traced threads while a trace is active"));
            return false;
        }
    }
    dputs(dbggettracethreads() ? QT_TRANSLATE_NOOP("DBG", "Trace threads: all") : QT_TRANSLATE_NOOP("DBG", "Trace threads: current"));
    return true;
}

bool cbDebugStartTraceRecording(int argc, char* argv[])
{
    if(IsArgumentsLessThan(argc, 2))
        return false;
    return TraceRecord.enableTraceRecording(true, argv[1]);
}

bool cbDebugStopTraceRecording(int argc, char* argv[])
{
    return TraceRecord.enableTraceRecording(false, nullptr);
}

bool cbDebugTraceExport(int argc, char* argv[])
{
    if(IsArgumentsLessThan(argc, 3))
        return false;
    // TraceExport trace, output[, format][, slide][, key=value...]
    TraceExportOptions options;
    String formatName;
    bool slideGiven = false;
    for(int i = 3; i < argc; i++)
    {
        String arg = argv[i];
        auto equal = arg.find('=');
        String key = equal == String::npos ? "" : StringUtils::ToLower(StringUtils::Trim(arg.substr(0, equal)));
        String value = equal == String::npos ? arg : StringUtils::Trim(arg.substr(equal + 1));
        duint number = 0;
        if(key.empty() && i == 3)
            formatName = StringUtils::ToLower(value);
        else if((key.empty() && i == 4) || key == "slide")
        {
            if(!valfromstring(value.c_str(), &number, false))
            {
                dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), value.c_str());
                return false;
            }
            options.slide = number;
            slideGiven = true;
        }
        else if(key == "format")
            formatName = StringUtils::ToLower(value);
        else if(key == "regs" || key == "registers")
        {
            if(!ftt::ParseLevels(value, options.registers))
            {
                dprintf(QT_TRANSLATE_NOOP("DBG", "Unknown register level \"%s\", use gpr, fpu, sse, avx, all or a combination such as fpu+sse\n"), value.c_str());
                return false;
            }
        }
        else if(key == "thread" || key == "snapshot")
        {
            if(!valfromstring(value.c_str(), &number, false) || number > 0xFFFFFFFF)
            {
                dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), value.c_str());
                return false;
            }
            if(key == "thread")
                options.thread = (uint32_t)number;
            else
                options.snapshotInterval = (uint32_t)number;
        }
        else
        {
            dprintf(QT_TRANSLATE_NOOP("DBG", "Unknown argument \"%s\" (options: regs=, thread=, snapshot=, slide=)\n"), argv[i]);
            return false;
        }
    }

    TraceExportFormat format = TraceExportFormat::Json;
    if(formatName.empty())
    {
        auto outputName = StringUtils::ToLower(argv[2]);
        if(StringUtils::EndsWith(outputName, ".csv"))
            format = TraceExportFormat::Csv;
        else if(StringUtils::EndsWith(outputName, ".tenet")) // extension used by the Frinet tracer
            format = TraceExportFormat::Frinet;
        else if(StringUtils::EndsWith(outputName, ".ftt"))
            format = TraceExportFormat::Ftt;
    }
    else if(formatName == "csv")
        format = TraceExportFormat::Csv;
    else if(formatName == "tenet")
        format = TraceExportFormat::Tenet;
    else if(formatName == "frinet")
        format = TraceExportFormat::Frinet;
    else if(formatName == "ftt")
        format = TraceExportFormat::Ftt;
    else if(formatName != "json")
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Unknown export format \"%s\", use \"json\", \"csv\", \"tenet\", \"frinet\" or \"ftt\"\n"), formatName.c_str());
        return false;
    }
    if(slideGiven && format != TraceExportFormat::Frinet)
    {
        dputs(QT_TRANSLATE_NOOP("DBG", "The slide argument is only used by the frinet format"));
        return false;
    }

    TraceExportResult result;
    if(!TraceExportFile(argv[1], argv[2], format, result, options))
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Trace export failed: %s\n"), result.error.c_str());
        return false;
    }
    if(result.truncated)
        dputs(QT_TRANSLATE_NOOP("DBG", "The last trace block is incomplete and was skipped (is the trace still being recorded?)"));
    if(format == TraceExportFormat::Frinet)
    {
        if(result.slide)
            dprintf(QT_TRANSLATE_NOOP("DBG", "ASLR slide: %p\n"), (void*)result.slide);
        else
            dputs(QT_TRANSLATE_NOOP("DBG", "No ASLR slide written (pass it as the 4th argument, for example mod.main())"));
    }
    varset("$result", result.count, false);
    dprintf(QT_TRANSLATE_NOOP("DBG", "Exported %llu instructions (%llu threads, %llu context switches) to %s\n"), (unsigned long long)result.count,
            (unsigned long long)result.threads, (unsigned long long)result.switches, argv[2]);
    if(format == TraceExportFormat::Ftt)
        dprintf(QT_TRANSLATE_NOOP("DBG", "%llu memory snapshots\n"), (unsigned long long)result.snapshots);
    if(result.skippedCalls)
        dprintf(QT_TRANSLATE_NOOP("DBG", "%llu calls ran untraced (TraceSetSkip)\n"), (unsigned long long)result.skippedCalls);
    // $result1: CPU time samples, $result2: intervals in which other threads ran outside the trace
    varset("$result1", result.samples, false);
    varset("$result2", result.concurrentSamples, false);
    if(result.samples)
        dprintf(QT_TRANSLATE_NOOP("DBG", "Thread order: %llu of %llu sampled intervals exact, %llu with other threads running outside the trace (approximate order)\n"),
                (unsigned long long)(result.samples - result.concurrentSamples), (unsigned long long)result.samples, (unsigned long long)result.concurrentSamples);
    return true;
}
