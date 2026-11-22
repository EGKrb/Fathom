// Emulation commands of ExtraCmds: run code of the debugged process (or of any minidump) in an
// emulator, with dumpulator (https://github.com/mrexodia/dumpulator, in emulator\ next to x32\ and
// x64\). The debuggee is never touched: its state is saved to a minidump (the minidump command), and
// the emulator runs from that copy, with the system calls implemented in Python.
//
//   emu.call address[, argument ...]   call a function; $result = its return value
//                                      arguments: expressions, str:TEXT, wstr:TEXT, buf:SIZE, bytes:HEX
//   emu.run [end[, count]]             run the current thread on (end 0: no end address); $result = the
//                                      address it stopped at, $result2 = the exit code if the process
//                                      exited (else -1)
//   emu.script file[, argument ...]    run a Python script with dp (the Dumpulator of the dump);
//                                      $result = result["return"] if the script sets it
//   ($result1: the number of instructions recorded when emu.trace is on)
//   emu.trace on|off                   record the emulated instructions (opened in Time Travel)
//   emu.syscalls on|off                show the system calls and exceptions of the emulated code
//   emu.strict on|off                  stop at a system call or instruction the emulator lacks (off: it
//                                      fails in the program, and the emulation goes on)
//   emu.cpus [n]                       processors of the emulated machine (0: those of the dump)
//   emu.seed [n]                       seed of the interleaving of the threads (the same run every time)
//   emu.network simulated|real         the network of the emulated program: simulated (names resolve to
//                                      192.0.2.x, connections succeed, HTTP answers 200), or real (the
//                                      host's network: only for a program you trust)
//   emu.screenshots on|off             save images of the windows of the emulated program (PNG, their
//                                      paths in the log)
//   emu.dump [file]                    save the debuggee for the emulator now (a file: keep it)
//   emu.load file / emu.unload         emulate a minidump from a file instead of the debuggee
//   emu.info                           threads and modules of the dump
//   emu.setup                          install the Python packages of the emulator (pip)

#include "emucmds.h"

#include "common.h"

#include "jansson/jansson.h"

#include <mutex>
#include <vector>

using namespace extracmds;

#define LOG_PREFIX EXTRACMDS_LOG_PREFIX

namespace
{
    struct EmuState
    {
        std::mutex lock;
        std::string dump;     // minidump the emulator loads
        bool owned = false;   // a dump of the debuggee made by these commands: deleted when the debuggee runs again
        bool trace = false;
        bool syscalls = false;
        bool strict = false;
        unsigned cpus = 0;    // 0: the processors of the dump
        unsigned seed = 1;
        bool realNetwork = false;
        bool screenshots = false;
        unsigned traces = 0;
    };
    EmuState gEmu;

    // emulator\fathom_emu.py: release\emulator in an archive, emulator\ at the root of the repository
    // for a development build (bin\x64\x64dbg.exe)
    std::wstring EmulatorDir()
    {
        auto root = X64dbgRoot();
        for(auto & candidate : { root + L"\\emulator", root + L"\\..\\emulator" })
        {
            auto folder = FullPath(Utf16ToUtf8(candidate));
            if(GetFileAttributesW((folder + L"\\fathom_emu.py").c_str()) != INVALID_FILE_ATTRIBUTES)
                return folder;
        }
        _plugin_logprintf(LOG_PREFIX "emulator not found (%s\\emulator\\fathom_emu.py)\n", Utf16ToUtf8(root).c_str());
        return {};
    }

    bool FileExists(const std::string & file)
    {
        auto attributes = GetFileAttributesW(Utf8ToUtf16(file).c_str());
        return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
    }

    // %TEMP%\Fathom\emu, created if needed
    std::string WorkDir()
    {
        wchar_t temp[MAX_PATH] = L"";
        GetTempPathW(_countof(temp), temp);
        std::wstring folder = std::wstring(temp) + L"Fathom";
        CreateDirectoryW(folder.c_str(), nullptr);
        folder += L"\\emu";
        CreateDirectoryW(folder.c_str(), nullptr);
        return Utf16ToUtf8(folder);
    }

    // Forget the dump; delete it when it is a temporary dump of the debuggee
    void ForgetDump(bool onlyOwned)
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        if(onlyOwned && !gEmu.owned)
            return;
        if(gEmu.owned && !gEmu.dump.empty())
            DeleteFileW(Utf8ToUtf16(gEmu.dump).c_str());
        gEmu.dump.clear();
        gEmu.owned = false;
    }

    // The debuggee saved with x64dbg's minidump command: full memory, handles, and the current thread
    // as the thread of the exception, which the emulator runs
    bool DumpDebuggee(const std::string & file)
    {
        if(!DbgIsDebugging())
        {
            _plugin_logputs(LOG_PREFIX "not debugging: start a process, or emulate a minidump file with emu.load");
            return false;
        }
        DeleteFileW(Utf8ToUtf16(file).c_str());
        if(!Exec("minidump " + Quote(file)) || !FileExists(file))
        {
            _plugin_logprintf(LOG_PREFIX "cannot save the debuggee to %s\n", file.c_str());
            return false;
        }
        return true;
    }

    // The dump to emulate: the file of emu.load / emu.dump, else a dump of the debuggee made now (kept
    // until the debuggee runs again). debuggee: the dump is the current state of the debuggee.
    bool CurrentDump(std::string & dump, bool & debuggee)
    {
        {
            std::lock_guard<std::mutex> guard(gEmu.lock);
            if(!gEmu.dump.empty() && FileExists(gEmu.dump))
            {
                dump = gEmu.dump;
                debuggee = gEmu.owned;
                return true;
            }
        }
        if(!DbgIsDebugging())
        {
            _plugin_logputs(LOG_PREFIX "nothing to emulate: debug a process, or load a minidump with emu.load file");
            return false;
        }
        auto file = WorkDir() + "\\process_" + std::to_string(DbgGetProcessId()) + ".dmp";
        _plugin_logputs(LOG_PREFIX "saving the debuggee for the emulator...");
        if(!DumpDebuggee(file))
            return false;
        std::lock_guard<std::mutex> guard(gEmu.lock);
        gEmu.dump = file;
        gEmu.owned = true;
        dump = file;
        debuggee = true;
        return true;
    }

    std::string ReadTextFile(const std::string & file)
    {
        std::string text;
        HANDLE handle = CreateFileW(Utf8ToUtf16(file).c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if(handle == INVALID_HANDLE_VALUE)
            return text;
        char buffer[4096];
        DWORD read = 0;
        while(ReadFile(handle, buffer, sizeof(buffer), &read, nullptr) && read)
            text.append(buffer, read);
        CloseHandle(handle);
        return text;
    }

    struct EmuResult
    {
        bool ok = false;
        std::string error;
        bool hasValue = false;
        duint value = 0;       // emu.call: the return value (emu.script: result["return"])
        duint cip = 0;         // where the emulation stopped
        duint exitCode = duint(-1); // the exit code if the emulated process exited
        duint instructions = 0;
        std::string trace;
        std::string arch;
    };

    duint HexMember(json_t* root, const char* name)
    {
        auto text = json_string_value(json_object_get(root, name));
        return text ? (duint)strtoull(text, nullptr, 16) : 0;
    }

    // fathom_emu.py --result FILE <action> <options> <dump> <arguments>, output to the log. The options
    // come before the positional arguments: those of a script take everything after its name.
    bool RunEmulator(const char* action, const std::vector<std::wstring> & arguments, EmuResult & result, bool needsDump = true)
    {
        auto folder = EmulatorDir();
        if(folder.empty())
            return false;
        std::wstring dumpArgument;
        bool debuggee = false;
        if(needsDump)
        {
            std::string dump;
            if(!CurrentDump(dump, debuggee))
                return false;
            dumpArgument = L" " + QuoteArgument(Utf8ToUtf16(dump));
        }
        bool trace, syscalls, strict, realNetwork, screenshots;
        unsigned index, cpus, seed;
        {
            std::lock_guard<std::mutex> guard(gEmu.lock);
            trace = gEmu.trace;
            syscalls = gEmu.syscalls;
            strict = gEmu.strict;
            cpus = gEmu.cpus;
            seed = gEmu.seed;
            realNetwork = gEmu.realNetwork;
            screenshots = gEmu.screenshots;
            index = ++gEmu.traces;
        }
        auto work = WorkDir();
        auto resultFile = work + "\\result_" + std::to_string(GetCurrentProcessId()) + ".json";
        DeleteFileW(Utf8ToUtf16(resultFile).c_str());
        std::wstring commandLine = L"--result " + QuoteArgument(Utf8ToUtf16(resultFile)) + L" " + Utf8ToUtf16(action);
        if(needsDump && strcmp(action, "info") != 0)
        {
            if(trace)
            {
                // the extension is .trace64 or .trace32, after the architecture of the dump
                auto file = work + "\\emu_" + std::to_string(GetCurrentProcessId()) + "_" + std::to_string(index);
                commandLine += L" --trace " + QuoteArgument(Utf8ToUtf16(file));
            }
            if(syscalls)
                commandLine += L" --syscalls";
            if(strict)
                commandLine += L" --strict";
            if(cpus)
                commandLine += L" --cpus " + std::to_wstring(cpus);
            commandLine += L" --seed " + std::to_wstring(seed);
            if(realNetwork)
                commandLine += L" --network real";
            if(screenshots)
                commandLine += L" --screenshots " + QuoteArgument(Utf8ToUtf16(work + "\\screenshots"));
            if(debuggee)
            {
                // the trace belongs to the debugged process: Time Travel names its addresses
                char hash[32];
                sprintf_s(hash, "0x%llX", (unsigned long long)DbgFunctions()->DbGetHash());
                commandLine += L" --hash " + Utf8ToUtf16(hash);
            }
        }
        commandLine += dumpArgument;
        for(auto & argument : arguments)
            commandLine += L" " + QuoteArgument(argument);
        RunPythonToLog(folder, L"fathom_emu.py", commandLine);

        auto text = ReadTextFile(resultFile);
        DeleteFileW(Utf8ToUtf16(resultFile).c_str());
        json_error_t error;
        json_t* root = text.empty() ? nullptr : json_loadb(text.c_str(), text.size(), 0, &error);
        if(!root)
        {
            _plugin_logputs(LOG_PREFIX "the emulator gave no result");
            return false;
        }
        result.ok = json_is_true(json_object_get(root, "ok"));
        if(auto message = json_string_value(json_object_get(root, "error")))
            result.error = message;
        result.hasValue = json_is_string(json_object_get(root, "return"));
        result.value = HexMember(root, "return");
        result.cip = HexMember(root, "cip");
        if(json_is_integer(json_object_get(root, "exit_code")))
            result.exitCode = (duint)json_integer_value(json_object_get(root, "exit_code"));
        result.instructions = (duint)json_integer_value(json_object_get(root, "instructions"));
        if(auto path = json_string_value(json_object_get(root, "trace")))
            result.trace = path;
        if(auto arch = json_string_value(json_object_get(root, "arch")))
            result.arch = arch;
        json_decref(root);
        return true;
    }

    void SetResult(const char* name, duint value)
    {
        char command[64];
        sprintf_s(command, "mov %s, 0x%llX", name, (unsigned long long)value);
        DbgCmdExecDirect(command);
    }

    // The trace of the emulated code: in the Time Travel tab (tt.* commands) and in x64dbg's trace view
    void OpenTrace(const EmuResult & result)
    {
        if(result.trace.empty() || result.instructions == 0)
            return;
#ifdef _WIN64
        const char* arch = "x64";
#else
        const char* arch = "x86";
#endif
        if(result.arch != arch)
        {
            _plugin_logprintf(LOG_PREFIX "the trace is %s code: open it in %s (tt.open %s)\n", result.arch.c_str(),
                              result.arch == "x64" ? "x64dbg" : "x32dbg", Quote(result.trace).c_str());
            return;
        }
        Exec("tt.open " + Quote(result.trace));
        GuiOpenTraceFile(result.trace.c_str());
    }

    // An argument of emu.call: str:/wstr:/buf:/bytes: as is, otherwise an expression of the debugger
    bool CallArgument(const char* text, std::wstring & argument)
    {
        for(auto prefix : { "str:", "wstr:", "buf:", "bytes:" })
        {
            if(_strnicmp(text, prefix, strlen(prefix)) == 0)
            {
                argument = Utf8ToUtf16(text);
                return true;
            }
        }
        bool success = false;
        duint value = DbgEval(text, &success);
        if(!success)
        {
            _plugin_logprintf(LOG_PREFIX "invalid expression \"%s\"\n", text);
            return false;
        }
        char hex[32];
        sprintf_s(hex, "0x%llX", (unsigned long long)value);
        argument = Utf8ToUtf16(hex);
        return true;
    }

    // An address: an expression of the debugger, or module!export resolved by the emulator (a minidump
    // loaded with emu.load is not the debuggee: the debugger cannot resolve its symbols)
    std::wstring AddressArgument(const char* text)
    {
        bool success = false;
        duint value = DbgEval(text, &success);
        if(!success)
            return Utf8ToUtf16(text);
        char hex[32];
        sprintf_s(hex, "0x%llX", (unsigned long long)value);
        return Utf8ToUtf16(hex);
    }

    bool CbEmuCall(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "usage: emu.call address[, argument ...]\n"
                                       "  arguments: expressions, str:TEXT (ANSI string), wstr:TEXT (UTF-16 string),\n"
                                       "             buf:SIZE (zeroed buffer, shown after the call), bytes:HEX");
            return false;
        }
        std::vector<std::wstring> arguments{ AddressArgument(argv[1]) };
        for(int i = 2; i < argc; i++)
        {
            std::wstring argument;
            if(!CallArgument(argv[i], argument))
                return false;
            arguments.push_back(argument);
        }
        EmuResult result;
        if(!RunEmulator("call", arguments, result))
            return false;
        OpenTrace(result);
        SetResult("$result", result.value);
        SetResult("$result1", result.instructions);
        return result.ok;
    }

    bool CbEmuRun(int argc, char* argv[])
    {
        std::vector<std::wstring> arguments;
        if(argc > 1 && *argv[1])
        {
            auto end = AddressArgument(argv[1]);
            if(end != L"0x0") // emu.run 0, count: no end address
            {
                arguments.push_back(L"--end");
                arguments.push_back(end);
            }
        }
        if(argc > 2)
        {
            bool success = false;
            duint count = DbgEval(argv[2], &success);
            if(!success)
            {
                _plugin_logprintf(LOG_PREFIX "invalid expression \"%s\"\n", argv[2]);
                return false;
            }
            arguments.push_back(L"--count");
            arguments.push_back(std::to_wstring(count));
        }
        EmuResult result;
        if(!RunEmulator("run", arguments, result))
            return false;
        OpenTrace(result);
        SetResult("$result", result.cip);
        SetResult("$result1", result.instructions);
        SetResult("$result2", result.exitCode);
        return result.ok;
    }

    bool CbEmuScript(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "usage: emu.script file.py[, argument ...]   (dp: the Dumpulator of the dump, args: the arguments)");
            return false;
        }
        std::vector<std::wstring> arguments{ FullPath(argv[1]) };
        for(int i = 2; i < argc; i++)
            arguments.push_back(Utf8ToUtf16(argv[i]));
        EmuResult result;
        if(!RunEmulator("script", arguments, result))
            return false;
        OpenTrace(result);
        SetResult("$result", result.hasValue ? result.value : result.cip);
        SetResult("$result1", result.instructions);
        return result.ok;
    }

    bool Toggle(int argc, char* argv[], bool & option, const char* name, const char* description)
    {
        if(argc > 1)
        {
            if(_stricmp(argv[1], "on") == 0 || strcmp(argv[1], "1") == 0)
                option = true;
            else if(_stricmp(argv[1], "off") == 0 || strcmp(argv[1], "0") == 0)
                option = false;
            else
            {
                _plugin_logprintf(LOG_PREFIX "usage: %s on|off\n", name);
                return false;
            }
        }
        _plugin_logprintf(LOG_PREFIX "%s: %s\n", description, option ? "on" : "off");
        SetResult("$result", option ? 1 : 0);
        return true;
    }

    bool CbEmuTrace(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return Toggle(argc, argv, gEmu.trace, "emu.trace", "trace of the emulated instructions (opened in Time Travel)");
    }

    bool CbEmuSyscalls(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return Toggle(argc, argv, gEmu.syscalls, "emu.syscalls", "system calls of the emulated code in the log");
    }

    bool CbEmuStrict(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return Toggle(argc, argv, gEmu.strict, "emu.strict", "stop at what the emulator lacks (system call, instruction)");
    }

    // emu.cpus [n] / emu.seed [n]: a number option; without an argument, its value
    bool NumberOption(int argc, char* argv[], unsigned & option, const char* name, const char* description)
    {
        if(argc > 1)
        {
            bool success = false;
            duint value = DbgEval(argv[1], &success);
            if(!success || value > 0xFFFFFFFF)
            {
                _plugin_logprintf(LOG_PREFIX "usage: %s [n]\n", name);
                return false;
            }
            option = (unsigned)value;
        }
        _plugin_logprintf(LOG_PREFIX "%s: %u\n", description, option);
        SetResult("$result", option);
        return true;
    }

    bool CbEmuCpus(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return NumberOption(argc, argv, gEmu.cpus, "emu.cpus", "processors of the emulated machine (0: those of the dump)");
    }

    bool CbEmuSeed(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return NumberOption(argc, argv, gEmu.seed, "emu.seed", "seed of the interleaving of the emulated threads");
    }

    bool CbEmuScreenshots(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        return Toggle(argc, argv, gEmu.screenshots, "emu.screenshots", "images of the windows of the emulated program");
    }

    bool CbEmuNetwork(int argc, char* argv[])
    {
        std::lock_guard<std::mutex> guard(gEmu.lock);
        if(argc > 1)
        {
            if(_stricmp(argv[1], "simulated") == 0)
                gEmu.realNetwork = false;
            else if(_stricmp(argv[1], "real") == 0)
                gEmu.realNetwork = true;
            else
            {
                _plugin_logputs(LOG_PREFIX "usage: emu.network simulated|real");
                return false;
            }
        }
        _plugin_logprintf(LOG_PREFIX "network of the emulated program: %s\n",
                          gEmu.realNetwork ? "real (the host's network)" : "simulated");
        SetResult("$result", gEmu.realNetwork ? 1 : 0);
        return true;
    }

    bool CbEmuDump(int argc, char* argv[])
    {
        bool keep = argc > 1;
        std::string file = keep ? Utf16ToUtf8(FullPath(argv[1])) : WorkDir() + "\\process_" + std::to_string(DbgGetProcessId()) + ".dmp";
        ForgetDump(false);
        if(!DumpDebuggee(file))
            return false;
        std::lock_guard<std::mutex> guard(gEmu.lock);
        gEmu.dump = file;
        gEmu.owned = !keep;
        _plugin_logprintf(LOG_PREFIX "the emulator uses %s%s\n", file.c_str(), keep ? "" : " (until the debuggee runs again)");
        return true;
    }

    bool CbEmuLoad(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "usage: emu.load file.dmp   (a minidump with full memory)");
            return false;
        }
        auto file = Utf16ToUtf8(FullPath(argv[1]));
        if(!FileExists(file))
        {
            _plugin_logprintf(LOG_PREFIX "file not found: %s\n", file.c_str());
            return false;
        }
        ForgetDump(false);
        std::lock_guard<std::mutex> guard(gEmu.lock);
        gEmu.dump = file;
        gEmu.owned = false;
        _plugin_logprintf(LOG_PREFIX "the emulator uses %s (emu.unload: back to the debuggee)\n", file.c_str());
        return true;
    }

    bool CbEmuUnload(int, char* [])
    {
        ForgetDump(false);
        _plugin_logputs(LOG_PREFIX "the emulator uses the debuggee");
        return true;
    }

    bool CbEmuInfo(int, char* [])
    {
        EmuResult result;
        return RunEmulator("info", {}, result) && result.ok;
    }

    bool CbEmuSetup(int, char* [])
    {
        EmuResult result;
        return RunEmulator("setup", {}, result, false) && result.ok;
    }

    // The debuggee changed: its dump is out of date
    void CbDebuggeeChanged(CBTYPE, void*)
    {
        ForgetDump(true);
    }
}

void EmuRegister(int pluginHandle)
{
    _plugin_registercommand(pluginHandle, "emu.call", CbEmuCall, false);
    _plugin_registercommand(pluginHandle, "emu.run", CbEmuRun, false);
    _plugin_registercommand(pluginHandle, "emu.script", CbEmuScript, false);
    _plugin_registercommand(pluginHandle, "emu.trace", CbEmuTrace, false);
    _plugin_registercommand(pluginHandle, "emu.syscalls", CbEmuSyscalls, false);
    _plugin_registercommand(pluginHandle, "emu.strict", CbEmuStrict, false);
    _plugin_registercommand(pluginHandle, "emu.cpus", CbEmuCpus, false);
    _plugin_registercommand(pluginHandle, "emu.seed", CbEmuSeed, false);
    _plugin_registercommand(pluginHandle, "emu.network", CbEmuNetwork, false);
    _plugin_registercommand(pluginHandle, "emu.screenshots", CbEmuScreenshots, false);
    _plugin_registercommand(pluginHandle, "emu.dump", CbEmuDump, true);
    _plugin_registercommand(pluginHandle, "emu.load", CbEmuLoad, false);
    _plugin_registercommand(pluginHandle, "emu.unload", CbEmuUnload, false);
    _plugin_registercommand(pluginHandle, "emu.info", CbEmuInfo, false);
    _plugin_registercommand(pluginHandle, "emu.setup", CbEmuSetup, false);
    _plugin_registercallback(pluginHandle, CB_RESUMEDEBUG, CbDebuggeeChanged);
    _plugin_registercallback(pluginHandle, CB_STOPDEBUG, CbDebuggeeChanged);
}
