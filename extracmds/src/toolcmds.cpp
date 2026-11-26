// Analysis tools driven from the debugger (ExtraCmds): NoVmp (VMProtect devirtualization, novmp\), angr and
// miasm (Python, analysis\). They work on the debuggee as it is now: its module image read from memory
// (NoVmp), or a minidump of the process made by the command (angr, miasm).
//
//   novmp [address[, rva ...]]           devirtualizes the VMProtect 3.x virtual machines of the module that
//                                        contains address (default: the selection, else cip): NoVmp for
//                                        x64, NoVmp32 for x86; rva: only these VM entries; vipbase:<value>
//                                        passed to -vipbase. $result = the routines lifted
//   angr.symbolic [address, size]        a memory range angr makes symbolic (no argument: forget them)
//   angr.explore find[, avoid ...]       explores the paths from the current thread's state to find, not
//                                        through avoid; $result = 1 if found (the input in the log)
//   angr.apply                           writes the input found into the debuggee
//   miasm.ir [address]                   miasm's IR of the function at address
//   miasm.symb [address[, end]]          symbolic execution of it by miasm: what it computes, simplified
//   analysis.setup                       installs the Python packages (pip install -r analysis\requirements.txt)
//   binja [address]                      opens the module that contains address, read from memory, in Binary
//                                        Ninja at address
//   remill.lift [address[, size]]        the function lifted to LLVM IR by remill
//   revng.decompile [address]            the function decompiled to C by rev.ng (built for Windows)
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
    // (raw = virtual), with the image base where it is loaded: what NoVmp and Binary Ninja read. PE32 and
    // PE32+; machine: the module's IMAGE_FILE_MACHINE_*
    bool DumpModule(duint address, const wchar_t* tool, std::string & file, duint & base, std::string & name, WORD & machine)
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
        IMAGE_NT_HEADERS32 nt32 = {};
        if(!DbgMemRead(base, &dos, sizeof(dos)) || dos.e_magic != IMAGE_DOS_SIGNATURE || !DbgMemRead(base + dos.e_lfanew, &nt32, sizeof(nt32)) ||
                nt32.Signature != IMAGE_NT_SIGNATURE)
        {
            _plugin_logprintf(LOG_PREFIX "no PE header at %s\n", Hex(base).c_str());
            return false;
        }
        machine = nt32.FileHeader.Machine;
        const bool pe64 = nt32.OptionalHeader.Magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC;
        const duint size = nt32.OptionalHeader.SizeOfImage; // same offset in PE32 and PE32+
        if(!size || size > 0x40000000)
        {
            _plugin_logprintf(LOG_PREFIX "unexpected image size %s\n", Hex(size).c_str());
            return false;
        }
        std::vector<uint8_t> image(size);
        for(duint page = 0; page < size; page += 0x1000)
            DbgMemRead(base + page, image.data() + page, (duint)std::min<duint>(0x1000, size - page)); // unreadable pages: zero
        IMAGE_SECTION_HEADER* section = nullptr;
        WORD sections = 0;
        if(pe64)
        {
            auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(image.data() + dos.e_lfanew);
            nt->OptionalHeader.ImageBase = base;
            nt->OptionalHeader.FileAlignment = nt->OptionalHeader.SectionAlignment;
            section = IMAGE_FIRST_SECTION(nt);
            sections = nt->FileHeader.NumberOfSections;
        }
        else
        {
            auto* nt = reinterpret_cast<IMAGE_NT_HEADERS32*>(image.data() + dos.e_lfanew);
            nt->OptionalHeader.ImageBase = (DWORD)base;
            nt->OptionalHeader.FileAlignment = nt->OptionalHeader.SectionAlignment;
            section = IMAGE_FIRST_SECTION(nt);
            sections = nt->FileHeader.NumberOfSections;
        }
        for(WORD i = 0; i < sections; i++, section++)
        {
            if(section->VirtualAddress >= size)
                continue;
            section->PointerToRawData = section->VirtualAddress;
            const DWORD virtualSize = section->Misc.VirtualSize ? section->Misc.VirtualSize : section->SizeOfRawData;
            section->SizeOfRawData = (DWORD)std::min<duint>(virtualSize, size - section->VirtualAddress);
        }
        auto folder = TempDir(tool) + "\\" + name + "_" + Hex(base);
        CreateDirectoryW(Utf8ToUtf16(folder).c_str(), nullptr);
        // the file name of the module (with its extension), else its name
        std::string fileName = name;
        char modulePath[MAX_PATH] = "";
        if(DbgFunctions()->ModPathFromAddr(base, modulePath, _countof(modulePath)) && *modulePath)
        {
            fileName = modulePath;
            const auto slash = fileName.find_last_of("\\/");
            if(slash != std::string::npos)
                fileName = fileName.substr(slash + 1);
        }
        file = folder + "\\" + fileName;
        std::ofstream out(Utf8ToUtf16(file), std::ios::binary);
        out.write(reinterpret_cast<const char*>(image.data()), image.size());
        return (bool)out;
    }

    // ---------------------------------------------------------------------------------- binja ---
    // Binary Ninja: binaryninja.exe of FATHOM_BINARYNINJA, else of its usual install folders
    std::wstring FindBinaryNinja()
    {
        wchar_t buffer[MAX_PATH] = L"";
        std::vector<std::wstring> candidates;
        if(GetEnvironmentVariableW(L"FATHOM_BINARYNINJA", buffer, _countof(buffer)))
            candidates.push_back(buffer);
        if(GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, _countof(buffer)))
            candidates.push_back(std::wstring(buffer) + L"\\Programs\\Vector35\\BinaryNinja\\binaryninja.exe");
        if(GetEnvironmentVariableW(L"ProgramFiles", buffer, _countof(buffer)))
            candidates.push_back(std::wstring(buffer) + L"\\Vector35\\BinaryNinja\\binaryninja.exe");
        for(auto & candidate : candidates)
        {
            auto path = candidate;
            if(GetFileAttributesW(path.c_str()) & FILE_ATTRIBUTE_DIRECTORY && GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES)
                path += L"\\binaryninja.exe";
            if(GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES)
                return path;
        }
        return {};
    }

    // binja [address]: the module that contains address, read from memory (as it is now, at its load
    // address), opened in Binary Ninja at address (its binaryninja: URL, which Binary Ninja confirms)
    bool CbBinja(int argc, char* argv[])
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        auto binja = FindBinaryNinja();
        if(binja.empty())
        {
            _plugin_logputs(LOG_PREFIX "Binary Ninja not found: install it, or set FATHOM_BINARYNINJA to its binaryninja.exe");
            return false;
        }
        std::string file, name;
        duint base = 0;
        WORD machine = 0;
        if(!DumpModule(address, L"binja", file, base, name, machine))
            return false;
        std::string url = "binaryninja:///";
        for(char ch : file)
            url.push_back(ch == '\\' ? '/' : ch);
        url += "?expr=" + Hex(address);
        std::wstring commandLine = QuoteArgument(binja) + L" " + QuoteArgument(Utf8ToUtf16(url));
        STARTUPINFOW si = { sizeof(si) };
        PROCESS_INFORMATION pi = {};
        if(!CreateProcessW(nullptr, &commandLine[0], nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi))
        {
            _plugin_logprintf(LOG_PREFIX "cannot start Binary Ninja (error %u)\n", GetLastError());
            return false;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        _plugin_logprintf(LOG_PREFIX "%s (read from memory at %s) opened in Binary Ninja at %s: %s\n", name.c_str(), Hex(base).c_str(),
                          Hex(address).c_str(), file.c_str());
        SetResult("$result", pi.dwProcessId);
        return true;
    }

    bool CbNoVmp(int argc, char* argv[])
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        std::string file, name;
        duint base = 0;
        WORD machine = 0;
        if(!DumpModule(address, L"novmp", file, base, name, machine))
            return false;
        if(machine != IMAGE_FILE_MACHINE_AMD64 && machine != IMAGE_FILE_MACHINE_I386)
        {
            _plugin_logputs(LOG_PREFIX "NoVmp devirtualizes x64 and x86 code: this module is neither");
            return false;
        }
        // NoVmp.exe for VMProtect's x64 virtual machine, NoVmp32.exe (Fathom's port) for the x86 one
        const bool x86 = machine == IMAGE_FILE_MACHINE_I386;
        const std::wstring novmp = x86 ? L"NoVmp32.exe" : L"NoVmp.exe";
        const std::wstring vtildump = x86 ? L"vtildump32.exe" : L"vtildump.exe";
        auto tools = FindFolder({ L"novmp", x86 ? L"..\\novmp\\build32" : L"..\\novmp\\build" }, novmp);
        if(tools.empty())
        {
            _plugin_logprintf(LOG_PREFIX "%s not found (novmp\\ of the release)\n", Utf16ToUtf8(novmp).c_str());
            return false;
        }
        std::wstring commandLine = QuoteArgument(tools + L"\\" + novmp) + L" " + QuoteArgument(Utf8ToUtf16(file));
        std::wstring vms;
        for(int i = 2; i < argc; i++)
        {
            // vipbase:<value>: the base VMProtect's encrypted VIP is relative to (NoVmp's -vipbase)
            if(_strnicmp(argv[i], "vipbase:", 8) == 0)
            {
                duint vipbase = 0;
                if(!EvalArgument(argv[i] + 8, vipbase))
                    return false;
                wchar_t text[48];
                swprintf_s(text, L" -vipbase %llx", (unsigned long long)vipbase);
                commandLine += text;
                continue;
            }
            duint rva = 0;
            if(!EvalArgument(argv[i], rva))
                return false;
            wchar_t text[32];
            swprintf_s(text, L" %llx", (unsigned long long)rva);
            vms += text;
        }
        if(!vms.empty())
            commandLine += L" -vms" + vms;
        _plugin_logprintf(LOG_PREFIX "NoVmp on %s (%s, image read from memory: %s)\n", name.c_str(), Hex(base).c_str(), file.c_str());
        const auto folder = Utf8ToUtf16(file.substr(0, file.find_last_of('\\')));
        RunToLog(commandLine, folder, novmp);
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
            if(routines.size() <= 4 && GetFileAttributesW((tools + L"\\" + vtildump).c_str()) != INVALID_FILE_ATTRIBUTES)
                for(auto & routine : routines)
                    RunToLog(QuoteArgument(tools + L"\\" + vtildump) + L" " + QuoteArgument(routine), folder, vtildump);
        }
        SetResult("$result", routines.size());
        return true;
    }

    // -------------------------------------------------------------------------------------- remill ---
    // remill.lift [address[, size]]: the function at address (x64dbg's analysis gives its end, else size
    // bytes, default 0x400) read from memory and lifted to LLVM IR by remill (remill\remill-lift-17.exe and
    // its semantics). The IR in the log and in %TEMP%\Fathom\remill\<address>.ll; $result = its lines
    bool CbRemillLift(int argc, char* argv[])
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        duint size = 0;
        if(argc > 2 && !EvalArgument(argv[2], size))
            return false;
        auto tools = FindFolder({ L"remill", L"..\\remill\\dist" }, L"remill-lift-17.exe");
        if(tools.empty())
        {
            _plugin_logputs(LOG_PREFIX "remill-lift-17.exe not found (remill\\ of the release)");
            return false;
        }
        duint start = 0, end = 0;
        if(!size && DbgFunctionGet(address, &start, &end) && start <= address && end > start)
        {
            address = start;
            size = end - start + 1;
        }
        if(!size)
            size = 0x400;
        if(size > 0x100000)
        {
            _plugin_logprintf(LOG_PREFIX "remill.lift: %s bytes is too much\n", Hex(size).c_str());
            return false;
        }
        std::vector<uint8_t> code((size_t)size);
        duint got = 0;
        for(; got < size; got += 0x100)
            if(!DbgMemRead(address + got, code.data() + got, (duint)std::min<duint>(0x100, size - got)))
                break;
        code.resize((size_t)std::min<duint>(got, size));
        if(code.empty())
        {
            _plugin_logprintf(LOG_PREFIX "remill.lift: no readable code at %s\n", Hex(address).c_str());
            return false;
        }
        std::string hex;
        static const char digits[] = "0123456789ABCDEF";
        for(auto b : code)
        {
            hex.push_back(digits[b >> 4]);
            hex.push_back(digits[b & 15]);
        }
        const auto out = TempDir(L"remill") + "\\" + Hex(address) + ".ll";
        DeleteFileW(Utf8ToUtf16(out).c_str());
#ifdef _WIN64
        const wchar_t* arch = L"amd64_avx";
#else
        const wchar_t* arch = L"x86_avx";
#endif
        std::wstring commandLine = QuoteArgument(tools + L"\\remill-lift-17.exe") + L" --arch " + arch + L" --os windows --address " +
                                   Utf8ToUtf16(Hex(address)) + L" --entry_address " + Utf8ToUtf16(Hex(address)) + L" --bytes " +
                                   Utf8ToUtf16(hex) + L" --ir_out " + QuoteArgument(Utf8ToUtf16(out));
        _plugin_logprintf(LOG_PREFIX "remill: %zu bytes at %s lifted to LLVM IR (%s)\n", code.size(), Hex(address).c_str(), Utf16ToUtf8(arch).c_str());
        RunToLog(commandLine, tools, L"remill-lift-17.exe");
        std::ifstream ir(Utf8ToUtf16(out));
        if(!ir)
        {
            _plugin_logputs(LOG_PREFIX "remill gave no IR (see above)");
            SetResult("$result", 0);
            return false;
        }
        std::string line;
        size_t lines = 0;
        while(std::getline(ir, line))
        {
            _plugin_logprintf("%s\n", line.c_str());
            lines++;
        }
        _plugin_logprintf(LOG_PREFIX "%zu lines of LLVM IR: %s\n", lines, out.c_str());
        SetResult("$result", lines);
        return true;
    }

    // --------------------------------------------------------------------------------------- rev.ng ---
    // rev.ng built for Windows (revng\): its Python (bin\python.exe) runs its command line (bin\revng)
    bool RunRevng(const std::wstring & revng, const std::wstring & arguments, const std::wstring & folder)
    {
        const std::wstring commandLine = QuoteArgument(revng + L"\\bin\\python.exe") + L" -X utf8 " + QuoteArgument(revng + L"\\bin\\revng") +
                                         L" " + arguments;
        // its bin\ first on the PATH of the child (its DLLs; psycopg loads libpq by name), then restored
        std::wstring previous;
        DWORD length = GetEnvironmentVariableW(L"PATH", nullptr, 0);
        if(length)
        {
            previous.resize(length);
            previous.resize(GetEnvironmentVariableW(L"PATH", &previous[0], length));
        }
        SetEnvironmentVariableW(L"PATH", (revng + L"\\bin;" + previous).c_str());
        const bool ok = RunToLog(commandLine, folder, L"revng");
        SetEnvironmentVariableW(L"PATH", length ? previous.c_str() : nullptr);
        return ok;
    }

    // revng.decompile [address]: the function that contains address (default: the selection, else cip), decompiled
    // to C by rev.ng. Its module is read from memory (as for novmp), rev.ng's project of it is created once
    // (revng project init: the initial auto-analysis of the whole module) in %TEMP%\Fathom\revng\<module>_<base>,
    // then the emit-c artifact of the function, without its markup. The C in the log and in <address>.c next to
    // the project; $result = its lines
    bool CbRevngDecompile(int argc, char* argv[])
    {
        duint address = 0;
        if(!TargetAddress(argc, argv, 1, address))
            return false;
        auto revng = FindFolder({ L"revng", L"..\\revng\\dist" }, L"bin\\revng");
        if(revng.empty())
        {
            _plugin_logputs(LOG_PREFIX "rev.ng not found (revng\\ of the release, or revng\\dist: revng\\build.ps1)");
            return false;
        }
        duint start = 0, end = 0;
        if(DbgFunctionGet(address, &start, &end) && start <= address && end >= address)
            address = start;
        std::string file, name;
        duint base = 0;
        WORD machine = 0;
        if(!DumpModule(address, L"revng", file, base, name, machine))
            return false;
        const auto slash = file.find_last_of('\\');
        const std::wstring project = Utf8ToUtf16(file.substr(0, slash));
        const std::wstring fileName = Utf8ToUtf16(file.substr(slash + 1));
        const bool x64 = machine == IMAGE_FILE_MACHINE_AMD64;
        if(!x64 && machine != IMAGE_FILE_MACHINE_I386)
        {
            _plugin_logprintf(LOG_PREFIX "rev.ng: %s is not an x86 / x64 module\n", name.c_str());
            return false;
        }
        // the project: once per module image (the initial auto-analysis takes a while on a large module)
        if(GetFileAttributesW((project + L"\\revng.yml").c_str()) == INVALID_FILE_ATTRIBUTES)
        {
            _plugin_logprintf(LOG_PREFIX "rev.ng: analysis of %s (read from memory at %s), once: %s\n", name.c_str(), Hex(base).c_str(),
                              Utf16ToUtf8(project).c_str());
            if(!RunRevng(revng, L"project init " + QuoteArgument(fileName), project) ||
                    GetFileAttributesW((project + L"\\revng.yml").c_str()) == INVALID_FILE_ATTRIBUTES)
            {
                _plugin_logputs(LOG_PREFIX "rev.ng could not analyze the module (see above)");
                SetResult("$result", 0);
                return false;
            }
        }
        char key[64];
        sprintf_s(key, "0x%llx:%s", (unsigned long long)address, x64 ? "Code_x86_64" : "Code_x86");
        const std::wstring ptml = project + L"\\" + Utf8ToUtf16(Hex(address)) + L".ptml";
        const std::wstring c = project + L"\\" + Utf8ToUtf16(Hex(address)) + L".c";
        DeleteFileW(ptml.c_str());
        DeleteFileW(c.c_str());
        _plugin_logprintf(LOG_PREFIX "rev.ng: decompiling %s\n", key);
        if(!RunRevng(revng, L"project artifact emit-c " + Utf8ToUtf16(key) + L" -o " + QuoteArgument(ptml), project) ||
                !RunRevng(revng, L"ptml -p -o " + QuoteArgument(c) + L" " + QuoteArgument(ptml), project))
        {
            _plugin_logprintf(LOG_PREFIX "rev.ng gave no C for %s (is it the entry of a function rev.ng found?)\n", key);
            SetResult("$result", 0);
            return false;
        }
        std::ifstream source(c);
        std::string line;
        size_t lines = 0;
        while(std::getline(source, line))
        {
            _plugin_logprintf("%s\n", line.c_str());
            lines++;
        }
        _plugin_logprintf(LOG_PREFIX "%zu lines of C: %s\n", lines, Utf16ToUtf8(c).c_str());
        SetResult("$result", lines);
        return lines != 0;
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
    _plugin_registercommand(pluginHandle, "binja", CbBinja, true);
    _plugin_registercommand(pluginHandle, "remill.lift", CbRemillLift, true);
    _plugin_registercommand(pluginHandle, "revng.decompile", CbRevngDecompile, true);
    _plugin_registercommand(pluginHandle, "angr.symbolic", CbAngrSymbolic, true);
    _plugin_registercommand(pluginHandle, "angr.explore", CbAngrExplore, true);
    _plugin_registercommand(pluginHandle, "angr.apply", CbAngrApply, true);
    _plugin_registercommand(pluginHandle, "miasm.ir", CbMiasmIr, true);
    _plugin_registercommand(pluginHandle, "miasm.symb", CbMiasmSymb, true);
    _plugin_registercommand(pluginHandle, "analysis.setup", CbAnalysisSetup, false);
    _plugin_registercallback(pluginHandle, CB_STOPDEBUG, CbDebuggeeStopped);
}
