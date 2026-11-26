// Analysis tools driven from the debugger (ExtraCmds): NoVmp (VMProtect devirtualization, novmp\), angr and
// miasm (Python, analysis\). They work on the debuggee as it is now: its module image read from memory
// (NoVmp), or a minidump of the process made by the command (angr, miasm).
//
//   novmp [address[, rva ...]]           devirtualizes the VMProtect 3.x virtual machines of the module that
//                                        contains address (default: the selection, else cip), x64; rva:
//                                        only these VM entries. $result = the routines lifted
//   angr.symbolic [address, size]        a memory range angr makes symbolic (no argument: forget them)
//   angr.explore find[, avoid ...]       explores the paths from the current thread's state to find, not
//                                        through avoid; $result = 1 if found (the input in the log)
//   angr.apply                           writes the input found into the debuggee
//   miasm.ir [address]                   miasm's IR of the function at address
//   miasm.symb [address[, end]]          symbolic execution of it by miasm: what it computes, simplified
//   analysis.setup                       installs the Python packages (pip install -r analysis\requirements.txt)
#include "toolcmds.h"

#include "common.h"

#include "jansson/jansson.h"

#include <fstream>
#include <sstream>
#include <vector>

using namespace extracmds;

#define LOG_PREFIX EXTRACMDS_LOG_PREFIX

namespace
{
    struct Range
    {
        duint address;
        duint size;
    };
    std::vector<Range> gSymbolic;                         // angr.symbolic
    std::vector<std::pair<duint, std::string>> gSolution; // angr.explore: address, bytes (hex)

    std::wstring FindFolder(const std::vector<std::wstring> & candidates, const std::wstring & file)
    {
        auto root = X64dbgRoot();
        for(auto & candidate : candidates)
        {
            auto folder = FullPath(Utf16ToUtf8(root + L"\\" + candidate));
            if(GetFileAttributesW((folder + L"\\" + file).c_str()) != INVALID_FILE_ATTRIBUTES)
                return folder;
        }
        return {};
    }

    // analysis\ in a release, ..\analysis for a development build (bin\x64)
    std::wstring AnalysisDir()
    {
        auto folder = FindFolder({ L"analysis", L"..\\analysis" }, L"fathom_miasm.py");
        if(folder.empty())
            _plugin_logputs(LOG_PREFIX "analysis\\ not found (fathom_angr.py, fathom_miasm.py)");
        return folder;
    }

    std::string TempDir(const wchar_t* name)
    {
        wchar_t temp[MAX_PATH] = L"";
        GetTempPathW(_countof(temp), temp);
        std::wstring folder = std::wstring(temp) + L"Fathom";
        CreateDirectoryW(folder.c_str(), nullptr);
        folder += std::wstring(L"\\") + name;
        CreateDirectoryW(folder.c_str(), nullptr);
        return Utf16ToUtf8(folder);
    }

    void SetResult(const char* name, duint value)
    {
        char command[64];
        sprintf_s(command, "mov %s, 0x%llX", name, (unsigned long long)value);
        DbgCmdExecDirect(command);
    }

    bool EvalArgument(const char* text, duint & value)
    {
        bool ok = false;
        value = DbgEval(text, &ok);
        if(!ok)
            _plugin_logprintf(LOG_PREFIX "invalid expression: %s\n", text);
        return ok;
    }

    // The address of a command: its argument, else the selection in the disassembly, else cip
    bool TargetAddress(int argc, char* argv[], int index, duint & address)
    {
        if(argc > index)
            return EvalArgument(argv[index], address);
        bool ok = false;
        address = DbgEval("dis.sel()", &ok);
        if(!ok || !address)
            address = DbgValFromString("cip");
        return true;
    }

    std::string Hex(duint value)
    {
        char text[32];
        sprintf_s(text, "0x%llX", (unsigned long long)value);
        return text;
    }

    // The debuggee saved now (x64dbg's minidump command): what angr and miasm load
    bool DumpNow(std::string & file)
    {
        if(!DbgIsDebugging())
        {
            _plugin_logputs(LOG_PREFIX "not debugging");
            return false;
        }
        file = TempDir(L"analysis") + "\\debuggee_" + std::to_string(GetCurrentProcessId()) + ".dmp";
        DeleteFileW(Utf8ToUtf16(file).c_str());
        if(!Exec("minidump " + Quote(file)) || GetFileAttributesW(Utf8ToUtf16(file).c_str()) == INVALID_FILE_ATTRIBUTES)
        {
            _plugin_logprintf(LOG_PREFIX "cannot save the debuggee to %s\n", file.c_str());
            return false;
        }
        return true;
    }

    json_t* ReadJson(const std::string & file)
    {
        std::ifstream f(Utf8ToUtf16(file), std::ios::binary);
        std::stringstream text;
        text << f.rdbuf();
        json_error_t error;
        return json_loads(text.str().c_str(), 0, &error);
    }

    // Runs a script of analysis\ on a dump of the debuggee; result: its JSON result (or nullptr)
    json_t* RunAnalysis(const wchar_t* script, const std::wstring & arguments)
    {
        auto folder = AnalysisDir();
        if(folder.empty())
            return nullptr;
        std::string dump;
        if(!DumpNow(dump))
            return nullptr;
        auto result = TempDir(L"analysis") + "\\result_" + std::to_string(GetCurrentProcessId()) + ".json";
        DeleteFileW(Utf8ToUtf16(result).c_str());
        const std::wstring commandLine = L"--result " + QuoteArgument(Utf8ToUtf16(result)) + L" " + arguments;
        // the dump goes where the script expects it: replace the %DUMP% placeholder
        std::wstring line = commandLine;
        auto at = line.find(L"%DUMP%");
        if(at != std::wstring::npos)
            line.replace(at, 6, QuoteArgument(Utf8ToUtf16(dump)));
        RunPythonToLog(folder, script, line);
        DeleteFileW(Utf8ToUtf16(dump).c_str());
        json_t* root = ReadJson(result);
        DeleteFileW(Utf8ToUtf16(result).c_str());
        if(!root)
        {
            _plugin_logprintf(LOG_PREFIX "%s gave no result (is the Python package installed? analysis.setup)\n", Utf16ToUtf8(script).c_str());
            return nullptr;
        }
        if(!json_is_true(json_object_get(root, "ok")))
        {
            const char* error = json_string_value(json_object_get(root, "error"));
            _plugin_logprintf(LOG_PREFIX "%s failed: %s\n", Utf16ToUtf8(script).c_str(), error ? error : "?");
        }
        return root;
    }

    std::wstring Arch()
    {
#ifdef _WIN64
        return L"x64";
#else
        return L"x86";
#endif
    }

    // --------------------------------------------------------------------------------------- novmp ---
    // The module image as it is in memory, made a PE file whose sections are at their virtual addresses
    // (raw = virtual), with the image base where it is loaded: what NoVmp reads
    bool DumpModule(duint address, std::string & file, duint & base, std::string & name)
    {
        bool ok = false;
        base = DbgEval(("mod.base(" + Hex(address) + ")").c_str(), &ok);
        if(!ok || !base)
        {
            _plugin_logprintf(LOG_PREFIX "no module at %s\n", Hex(address).c_str());
            return false;
        }
        char moduleName[MAX_MODULE_SIZE] = "";
        DbgGetModuleAt(base, moduleName);
        name = moduleName;
        IMAGE_DOS_HEADER dos = {};
        IMAGE_NT_HEADERS64 nt = {};
        if(!DbgMemRead(base, &dos, sizeof(dos)) || dos.e_magic != IMAGE_DOS_SIGNATURE || !DbgMemRead(base + dos.e_lfanew, &nt, sizeof(nt)) ||
                nt.Signature != IMAGE_NT_SIGNATURE)
        {
            _plugin_logprintf(LOG_PREFIX "no PE header at %s\n", Hex(base).c_str());
            return false;
        }
        if(nt.FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64)
        {
            _plugin_logputs(LOG_PREFIX "NoVmp devirtualizes x64 code: this module is not x64");
            return false;
        }
        const duint size = nt.OptionalHeader.SizeOfImage;
        std::vector<uint8_t> image(size);
        for(duint page = 0; page < size; page += 0x1000)
            DbgMemRead(base + page, image.data() + page, (duint)std::min<duint>(0x1000, size - page)); // unreadable pages: zero
        auto* ntOut = reinterpret_cast<IMAGE_NT_HEADERS64*>(image.data() + dos.e_lfanew);
        ntOut->OptionalHeader.ImageBase = base;
        ntOut->OptionalHeader.FileAlignment = ntOut->OptionalHeader.SectionAlignment;
        auto* section = IMAGE_FIRST_SECTION(ntOut);
        for(WORD i = 0; i < ntOut->FileHeader.NumberOfSections; i++, section++)
        {
            section->PointerToRawData = section->VirtualAddress;
            const DWORD virtualSize = section->Misc.VirtualSize ? section->Misc.VirtualSize : section->SizeOfRawData;
            section->SizeOfRawData = (DWORD)std::min<duint>(virtualSize, size - section->VirtualAddress);
        }
        auto folder = TempDir(L"novmp") + "\\" + name + "_" + Hex(base);
        CreateDirectoryW(Utf8ToUtf16(folder).c_str(), nullptr);
        file = folder + "\\" + name;
        std::ofstream out(Utf8ToUtf16(file), std::ios::binary);
        out.write(reinterpret_cast<const char*>(image.data()), image.size());
        return (bool)out;
    }

    bool CbNoVmp(int argc, char* argv[])
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        auto tools = FindFolder({ L"novmp", L"..\\novmp\\build" }, L"NoVmp.exe");
        if(tools.empty())
        {
            _plugin_logputs(LOG_PREFIX "NoVmp.exe not found (novmp\\ of the release)");
            return false;
        }
        std::string file, name;
        duint base = 0;
        if(!DumpModule(address, file, base, name))
            return false;
        std::wstring commandLine = QuoteArgument(tools + L"\\NoVmp.exe") + L" " + QuoteArgument(Utf8ToUtf16(file));
        if(argc > 2)
        {
            commandLine += L" -vms";
            for(int i = 2; i < argc; i++)
            {
                duint rva = 0;
                if(!EvalArgument(argv[i], rva))
                    return false;
                wchar_t text[32];
                swprintf_s(text, L" %llx", (unsigned long long)rva);
                commandLine += text;
            }
        }
        _plugin_logprintf(LOG_PREFIX "NoVmp on %s (%s, image read from memory: %s)\n", name.c_str(), Hex(base).c_str(), file.c_str());
        const auto folder = Utf8ToUtf16(file.substr(0, file.find_last_of('\\')));
        RunToLog(commandLine, folder, L"NoVmp.exe");
        // the routines NoVmp lifted: vms\<rva>.optimized.vtil
        std::vector<std::wstring> routines;
        WIN32_FIND_DATAW find = {};
        HANDLE h = FindFirstFileW((folder + L"\\vms\\*.optimized.vtil").c_str(), &find);
        if(h != INVALID_HANDLE_VALUE)
        {
            do
                routines.push_back(folder + L"\\vms\\" + find.cFileName);
            while(FindNextFileW(h, &find));
            FindClose(h);
        }
        if(routines.empty())
            _plugin_logprintf(LOG_PREFIX "no VMProtect virtual machine lifted in %s\n", name.c_str());
        else
        {
            _plugin_logprintf(LOG_PREFIX "%zu routines lifted in %s\\vms\n", routines.size(), Utf16ToUtf8(folder).c_str());
            // their VTIL as text (vtildump), when there are few
            if(routines.size() <= 4 && GetFileAttributesW((tools + L"\\vtildump.exe").c_str()) != INVALID_FILE_ATTRIBUTES)
                for(auto & routine : routines)
                    RunToLog(QuoteArgument(tools + L"\\vtildump.exe") + L" " + QuoteArgument(routine), folder, L"vtildump.exe");
        }
        SetResult("$result", routines.size());
        return true;
    }

    // ---------------------------------------------------------------------------------------- angr ---
    bool CbAngrSymbolic(int argc, char* argv[])
    {
        if(argc < 3)
        {
            gSymbolic.clear();
            _plugin_logputs(LOG_PREFIX "angr: no symbolic memory");
            return true;
        }
        Range range = {};
        if(!EvalArgument(argv[1], range.address) || !EvalArgument(argv[2], range.size))
            return false;
        gSymbolic.push_back(range);
        _plugin_logprintf(LOG_PREFIX "angr: %llu bytes at %s symbolic (%zu ranges)\n", (unsigned long long)range.size, Hex(range.address).c_str(), gSymbolic.size());
        return true;
    }

    bool CbAngrExplore(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "angr.explore find[, avoid ...]");
            return false;
        }
        duint find = 0;
        if(!EvalArgument(argv[1], find))
            return false;
        std::wstring arguments = L"explore %DUMP% --thread " + std::to_wstring(DbgGetThreadId()) + L" --find " + Utf8ToUtf16(Hex(find));
        for(int i = 2; i < argc; i++)
        {
            duint avoid = 0;
            if(!EvalArgument(argv[i], avoid))
                return false;
            arguments += L" --avoid " + Utf8ToUtf16(Hex(avoid));
        }
        for(auto & range : gSymbolic)
            arguments += L" --symbolic " + Utf8ToUtf16(Hex(range.address) + ":" + Hex(range.size));
        json_t* root = RunAnalysis(L"fathom_angr.py", arguments);
        bool found = false;
        gSolution.clear();
        if(root)
        {
            found = json_is_true(json_object_get(root, "found"));
            json_t* solution = json_object_get(root, "solution");
            size_t index;
            json_t* item;
            json_array_foreach(solution, index, item)
            {
                const char* address = json_string_value(json_object_get(item, "address"));
                const char* bytes = json_string_value(json_object_get(item, "bytes"));
                if(address && bytes)
                    gSolution.emplace_back((duint)strtoull(address, nullptr, 16), bytes);
            }
            json_decref(root);
        }
        SetResult("$result", found ? 1 : 0);
        return root != nullptr;
    }

    bool CbAngrApply(int, char* [])
    {
        if(gSolution.empty())
        {
            _plugin_logputs(LOG_PREFIX "angr.apply: no input found (angr.explore first)");
            return false;
        }
        size_t written = 0;
        for(auto & s : gSolution)
        {
            std::vector<uint8_t> data;
            for(size_t i = 0; i + 1 < s.second.size(); i += 2)
                data.push_back((uint8_t)strtoul(s.second.substr(i, 2).c_str(), nullptr, 16));
            if(DbgMemWrite(s.first, data.data(), data.size()))
                written += data.size();
        }
        GuiUpdateAllViews();
        _plugin_logprintf(LOG_PREFIX "angr: %zu bytes of the input written into the debuggee\n", written);
        SetResult("$result", written);
        return true;
    }

    // --------------------------------------------------------------------------------------- miasm ---
    bool Miasm(int argc, char* argv[], bool symbolic)
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        std::wstring arguments = std::wstring(symbolic ? L"symb" : L"ir") + L" %DUMP% " + Utf8ToUtf16(Hex(address)) + L" --arch " + Arch();
        if(symbolic && argc > 2)
        {
            duint end = 0;
            if(!EvalArgument(argv[2], end))
                return false;
            arguments += L" --end " + Utf8ToUtf16(Hex(end));
        }
        json_t* root = RunAnalysis(L"fathom_miasm.py", arguments);
        if(!root)
            return false;
        const bool ok = json_is_true(json_object_get(root, "ok"));
        SetResult("$result", (duint)json_integer_value(json_object_get(root, symbolic ? "changed" : "blocks")));
        json_decref(root);
        return ok;
    }

    bool CbMiasmIr(int argc, char* argv[]) { return Miasm(argc, argv, false); }
    bool CbMiasmSymb(int argc, char* argv[]) { return Miasm(argc, argv, true); }

    bool CbAnalysisSetup(int, char* [])
    {
        auto folder = AnalysisDir();
        auto python = FindPython();
        if(folder.empty() || python.empty())
        {
            _plugin_logputs(LOG_PREFIX "analysis.setup needs Python on the PATH and the analysis\\ folder");
            return false;
        }
        // --user outside a virtual environment, as emu.setup does
        const bool venv = GetEnvironmentVariableW(L"VIRTUAL_ENV", nullptr, 0) != 0;
        std::wstring commandLine = QuoteArgument(python) + L" -m pip install " + (venv ? L"" : L"--user ") + L"-r " + QuoteArgument(folder + L"\\requirements.txt");
        return RunToLog(commandLine, folder, L"pip");
    }

    void CbDebuggeeStopped(CBTYPE, void*)
    {
        gSymbolic.clear();
        gSolution.clear();
    }
}

void ToolsRegister(int pluginHandle)
{
    _plugin_registercommand(pluginHandle, "novmp", CbNoVmp, true);
    _plugin_registercommand(pluginHandle, "angr.symbolic", CbAngrSymbolic, true);
    _plugin_registercommand(pluginHandle, "angr.explore", CbAngrExplore, true);
    _plugin_registercommand(pluginHandle, "angr.apply", CbAngrApply, true);
    _plugin_registercommand(pluginHandle, "miasm.ir", CbMiasmIr, true);
    _plugin_registercommand(pluginHandle, "miasm.symb", CbMiasmSymb, true);
    _plugin_registercommand(pluginHandle, "analysis.setup", CbAnalysisSetup, false);
    _plugin_registercallback(pluginHandle, CB_STOPDEBUG, CbDebuggeeStopped);
}
