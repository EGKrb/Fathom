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
    TraceExportFormat format = TraceExportFormat::Json;
    String formatName = StringUtils::ToLower(argc > 3 ? argv[3] : "");
    if(formatName.empty())
    {
        auto outputName = StringUtils::ToLower(argv[2]);
        if(StringUtils::EndsWith(outputName, ".csv"))
            format = TraceExportFormat::Csv;
        else if(StringUtils::EndsWith(outputName, ".tenet")) // extension used by the Frinet tracer
            format = TraceExportFormat::Frinet;
    }
    else if(formatName == "csv")
        format = TraceExportFormat::Csv;
    else if(formatName == "tenet")
        format = TraceExportFormat::Tenet;
    else if(formatName == "frinet")
        format = TraceExportFormat::Frinet;
    else if(formatName != "json")
    {
        dprintf(QT_TRANSLATE_NOOP("DBG", "Unknown export format \"%s\", use \"json\", \"csv\", \"tenet\" or \"frinet\"\n"), argv[3]);
        return false;
    }

    duint slide = 0;
    if(argc > 4)
    {
        if(format != TraceExportFormat::Frinet)
        {
            dputs(QT_TRANSLATE_NOOP("DBG", "The slide argument is only used by the frinet format"));
            return false;
        }
        if(!valfromstring(argv[4], &slide, false))
        {
            dprintf(QT_TRANSLATE_NOOP("DBG", "Invalid expression \"%s\"\n"), argv[4]);
            return false;
        }
    }

    TraceExportResult result;
    if(!TraceExportFile(argv[1], argv[2], format, result, slide))
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
    dprintf(QT_TRANSLATE_NOOP("DBG", "Exported %llu instructions to %s\n"), (unsigned long long)result.count, argv[2]);
    return true;
}