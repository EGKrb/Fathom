// ExtraCmds: convenience commands for x64dbg.
//
//   .init file[, arguments[, folder]]   init for an executable, opens the minidump viewer for a .dmp,
//                                       wineye for a kernel crash dump or a raw memory image
//   .save [file[, address[, size]]]     dbsave / minidump / savedata depending on the arguments
//   scylla_hide.enable [profile]        activate a ScyllaHide profile (default: the last active one)
//   scylla_hide.disable                 switch ScyllaHide to its "Disabled" profile
//   scylla_hide.status                  show the active ScyllaHide profile and the available ones
//   hvkd [action [options]]             run HvKdFusion (Hyper-V kernel debugging / memory forensics) in a console
//   hvkd.log action [options]           run an HvKdFusion action without a window, output in the log
//   hvkd.check [--deep]                 HvKdFusion readiness table in the log
//   wineye file | wineye --options      open a kernel dump / memory image (or a live Hyper-V VM) in wineye

#include "_plugins.h"

#include <string>
#include <vector>

#define PLUGIN_NAME "ExtraCmds"
#define PLUGIN_VERSION 2
#define LOG_PREFIX "[" PLUGIN_NAME "] "

namespace
{
    int gPluginHandle;

    const char* const ScyllaHidePluginName = "ScyllaHideX64DBGPlugin";
    const wchar_t* const ScyllaHideDisabledProfile = L"Disabled";
    const wchar_t* const ScyllaHideDefaultProfile = L"Basic";

    std::wstring Utf8ToUtf16(const std::string & text)
    {
        if(text.empty())
            return {};
        int size = MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0);
        std::wstring result(size, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size);
        return result;
    }

    std::string Utf16ToUtf8(const std::wstring & text)
    {
        if(text.empty())
            return {};
        int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
        std::string result(size, '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size, nullptr, nullptr);
        return result;
    }

    // Quote an argument for an x64dbg command (same rules as Command::Escape)
    std::string Quote(const std::string & argument)
    {
        std::string result = "\"";
        size_t i = 0;
        while(i < argument.size())
        {
            char ch = argument[i];
            if(ch != '\\')
            {
                if(ch == '"' || ch == '{')
                    result.push_back('\\');
                result.push_back(ch);
                i++;
                continue;
            }
            size_t start = i;
            while(i < argument.size() && argument[i] == '\\')
                i++;
            size_t count = i - start;
            if(i == argument.size())
                result.append(count * 2, '\\');
            else if(argument[i] == '"' || argument[i] == '{')
            {
                result.append(count * 2 + 1, '\\');
                result.push_back(argument[i++]);
            }
            else
                result.append(count, '\\');
        }
        return result + "\"";
    }

    bool Exec(const std::string & command)
    {
        return DbgCmdExecDirect(command.c_str());
    }

    bool HasExtension(const std::string & file, std::initializer_list<const char*> extensions)
    {
        auto dot = file.find_last_of('.');
        if(dot == std::string::npos || file.find_first_of("\\/", dot) != std::string::npos)
            return false;
        auto extension = file.substr(dot);
        for(auto candidate : extensions)
            if(_stricmp(extension.c_str(), candidate) == 0)
                return true;
        return false;
    }

    bool IsDumpFile(const std::string & file)
    {
        return HasExtension(file, { ".dmp", ".mdmp", ".hdmp" });
    }

    // Folder that contains x32\ and x64\ (parent of the folder of the running executable)
    std::wstring X64dbgRoot()
    {
        wchar_t path[MAX_PATH] = L"";
        GetModuleFileNameW(nullptr, path, _countof(path));
        std::wstring result = path;
        for(int i = 0; i < 2; i++)
        {
            auto slash = result.find_last_of(L'\\');
            if(slash == std::wstring::npos)
                return L".";
            result.resize(slash);
        }
        return result;
    }

    std::wstring ArchFolder()
    {
        wchar_t path[MAX_PATH] = L"";
        GetModuleFileNameW(nullptr, path, _countof(path));
        std::wstring result = path;
        result.resize(result.find_last_of(L'\\'));
        return result;
    }

    // Kernel crash dump (MEMORY.DMP: "PAGEDUMP" for 32-bit, "PAGEDU64" for 64-bit) or raw physical memory
    // image: the memory of a whole machine, not of a process, that only a memory-forensics tool can read
    bool IsMemoryImage(const std::string & file)
    {
        if(HasExtension(file, { ".raw", ".mem", ".vmem" }))
            return true;
        if(!IsDumpFile(file))
            return false;
        HANDLE hFile = CreateFileW(Utf8ToUtf16(file).c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
        if(hFile == INVALID_HANDLE_VALUE)
            return false;
        char signature[8] = {};
        DWORD read = 0;
        bool ok = ReadFile(hFile, signature, sizeof(signature), &read, nullptr) && read == sizeof(signature);
        CloseHandle(hFile);
        return ok && (memcmp(signature, "PAGEDUMP", 8) == 0 || memcmp(signature, "PAGEDU64", 8) == 0);
    }

    // Quote an argument for a Windows command line (CommandLineToArgvW rules)
    std::wstring QuoteArgument(const std::wstring & argument)
    {
        if(!argument.empty() && argument.find_first_of(L" \t\"") == std::wstring::npos)
            return argument;
        std::wstring result = L"\"";
        size_t backslashes = 0;
        for(auto ch : argument)
        {
            if(ch == L'\\')
            {
                backslashes++;
                continue;
            }
            result.append(ch == L'"' ? backslashes * 2 + 1 : backslashes, L'\\');
            backslashes = 0;
            result.push_back(ch);
        }
        result.append(backslashes * 2, L'\\');
        return result + L"\"";
    }

    std::wstring FullPath(const std::string & file)
    {
        auto path = Utf8ToUtf16(file);
        wchar_t full[MAX_PATH] = L"";
        if(GetFullPathNameW(path.c_str(), _countof(full), full, nullptr))
            return full;
        return path;
    }

    // Everything after the command name, as typed (argv[0] is the whole command line)
    std::string RawArguments(const char* commandLine)
    {
        std::string text = commandLine;
        auto space = text.find_first_of(" \t");
        if(space == std::string::npos)
            return {};
        auto start = text.find_first_not_of(" \t", space);
        return start == std::string::npos ? std::string() : text.substr(start);
    }

    // HvKdFusion (Python tools): release\hvkdfusion in an archive, hvkdfusion\ at the root of the
    // repository for a development build (bin\x64\x64dbg.exe)
    std::wstring HvKdFusionDir()
    {
        auto root = X64dbgRoot();
        for(auto & candidate : { root + L"\\hvkdfusion", root + L"\\..\\hvkdfusion" })
        {
            auto folder = FullPath(Utf16ToUtf8(candidate));
            if(GetFileAttributesW((folder + L"\\hvkdfusion.py").c_str()) != INVALID_FILE_ATTRIBUTES)
                return folder;
        }
        _plugin_logprintf(LOG_PREFIX "HvKdFusion not found (%s\\hvkdfusion\\hvkdfusion.py)\n", Utf16ToUtf8(root).c_str());
        return {};
    }

    // hvkd.ps1 in a new console window: it asks for elevation (UAC) when the action needs it and keeps
    // the window open at the end (HVKD_KEEP_OPEN)
    bool LaunchHvKd(const std::wstring & arguments)
    {
        auto folder = HvKdFusionDir();
        if(folder.empty())
            return false;
        std::wstring commandLine = L"powershell.exe -NoProfile -ExecutionPolicy Bypass -File " + QuoteArgument(folder + L"\\hvkd.ps1");
        if(!arguments.empty())
            commandLine += L" " + arguments;
        STARTUPINFOW si = { sizeof(si) };
        PROCESS_INFORMATION pi = {};
        SetEnvironmentVariableW(L"HVKD_KEEP_OPEN", L"1");
        BOOL started = CreateProcessW(nullptr, &commandLine[0], nullptr, nullptr, FALSE, CREATE_NEW_CONSOLE, nullptr, folder.c_str(), &si, &pi);
        DWORD error = GetLastError();
        SetEnvironmentVariableW(L"HVKD_KEEP_OPEN", nullptr);
        if(!started)
        {
            _plugin_logprintf(LOG_PREFIX "cannot start PowerShell (error %u)\n", error);
            return false;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        _plugin_logprintf(LOG_PREFIX "HvKdFusion started in a new window: hvkd.ps1 %s\n", Utf16ToUtf8(arguments).c_str());
        return true;
    }

    std::wstring FindPython()
    {
        wchar_t path[MAX_PATH] = L"";
        for(auto name : { L"python.exe", L"py.exe" })
            if(SearchPathW(nullptr, name, nullptr, _countof(path), path, nullptr))
                return path;
        return {};
    }

    // hvkdfusion.py without a window, its output written to the log. Standard input is NUL, so an
    // interactive action (menu, wineye REPL) stops at its first prompt instead of blocking x64dbg
    bool RunHvKdToLog(const std::wstring & arguments)
    {
        auto folder = HvKdFusionDir();
        if(folder.empty())
            return false;
        auto python = FindPython();
        if(python.empty())
        {
            _plugin_logputs(LOG_PREFIX "Python not found on the PATH (python.exe or py.exe)");
            return false;
        }
        std::wstring commandLine = QuoteArgument(python) + L" -X utf8 " + QuoteArgument(folder + L"\\hvkdfusion.py") + L" " + arguments;
        SECURITY_ATTRIBUTES sa = { sizeof(sa), nullptr, TRUE };
        HANDLE readPipe = nullptr, writePipe = nullptr;
        if(!CreatePipe(&readPipe, &writePipe, &sa, 0))
            return false;
        SetHandleInformation(readPipe, HANDLE_FLAG_INHERIT, 0);
        HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);
        STARTUPINFOW si = { sizeof(si) };
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = nul;
        si.hStdOutput = writePipe;
        si.hStdError = writePipe;
        PROCESS_INFORMATION pi = {};
        BOOL started = CreateProcessW(nullptr, &commandLine[0], nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, folder.c_str(), &si, &pi);
        DWORD error = GetLastError();
        CloseHandle(writePipe);
        if(nul != INVALID_HANDLE_VALUE)
            CloseHandle(nul);
        if(!started)
        {
            CloseHandle(readPipe);
            _plugin_logprintf(LOG_PREFIX "cannot start Python (error %u)\n", error);
            return false;
        }
        std::string output;
        char buffer[4096];
        DWORD read = 0;
        while(ReadFile(readPipe, buffer, sizeof(buffer), &read, nullptr) && read)
            output.append(buffer, read);
        CloseHandle(readPipe);
        WaitForSingleObject(pi.hProcess, INFINITE);
        DWORD exitCode = 1;
        GetExitCodeProcess(pi.hProcess, &exitCode);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        for(size_t start = 0; start < output.size();)
        {
            auto end = output.find('\n', start);
            if(end == std::string::npos)
                end = output.size();
            auto line = output.substr(start, end - start);
            while(!line.empty() && line.back() == '\r')
                line.pop_back();
            auto progress = line.find_last_of('\r'); // progress counters rewrite the line with \r: keep the last state
            if(progress != std::string::npos)
                line = line.substr(progress + 1);
            _plugin_logprintf("%s\n", line.c_str());
            start = end + 1;
        }
        if(exitCode != 0)
            _plugin_logprintf(LOG_PREFIX "hvkdfusion.py exited with code %u\n", exitCode);
        return exitCode == 0;
    }

    bool OpenInWineye(const std::string & imageFile)
    {
        if(GetFileAttributesW(Utf8ToUtf16(imageFile).c_str()) == INVALID_FILE_ATTRIBUTES)
        {
            _plugin_logprintf(LOG_PREFIX "file not found: %s\n", imageFile.c_str());
            return false;
        }
        _plugin_logprintf(LOG_PREFIX "%s holds the memory of a whole machine, not of a process: x64dbg cannot debug it. "
                                     "Opening it in wineye (HvKdFusion, needs MemProcFS: hvkd setup --memprocfs-exe ...).\n", imageFile.c_str());
        return LaunchHvKd(L"wineye --dump " + QuoteArgument(FullPath(imageFile)));
    }

    bool OpenInMinidumpViewer(const std::string & dumpFile)
    {
        auto dumpPath = Utf8ToUtf16(dumpFile);
        if(GetFileAttributesW(dumpPath.c_str()) == INVALID_FILE_ATTRIBUTES)
        {
            _plugin_logprintf(LOG_PREFIX "file not found: %s\n", dumpFile.c_str());
            return false;
        }
        // The viewer is a 64-bit Qt application next to x64dbg.exe; it also reads dumps of 32-bit processes
        auto viewer = X64dbgRoot() + L"\\x64\\minidump.exe";
        if(GetFileAttributesW(viewer.c_str()) == INVALID_FILE_ATTRIBUTES)
        {
            _plugin_logprintf(LOG_PREFIX "minidump viewer not found: %s\n", Utf16ToUtf8(viewer).c_str());
            return false;
        }
        std::wstring commandLine = L"\"" + viewer + L"\" \"" + dumpPath + L"\"";
        STARTUPINFOW si = { sizeof(si) };
        PROCESS_INFORMATION pi = {};
        auto viewerFolder = viewer.substr(0, viewer.find_last_of(L'\\'));
        if(!CreateProcessW(viewer.c_str(), &commandLine[0], nullptr, nullptr, FALSE, 0, nullptr, viewerFolder.c_str(), &si, &pi))
        {
            _plugin_logprintf(LOG_PREFIX "cannot start the minidump viewer (error %u)\n", GetLastError());
            return false;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        _plugin_logprintf(LOG_PREFIX "a dump is not a running process: x64dbg cannot debug it. "
                                     "Opened %s in the minidump viewer (memory map, disassembly, hex dump, threads).\n", dumpFile.c_str());
        return true;
    }

    // .init file[, arguments[, folder]]
    bool CbInit(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "usage: .init file[, arguments[, folder]]   (a minidump opens the minidump viewer, "
                                       "a kernel crash dump or a .raw/.mem memory image opens wineye)");
            return false;
        }
        if(IsMemoryImage(argv[1]))
            return OpenInWineye(argv[1]);
        if(IsDumpFile(argv[1]))
            return OpenInMinidumpViewer(argv[1]);
        std::string command = "init " + Quote(argv[1]);
        for(int i = 2; i < argc && i < 4; i++)
            command += ", " + Quote(argv[i]);
        return Exec(command);
    }

    // .save [file[, address[, size]]]
    bool CbSave(int argc, char* argv[])
    {
        switch(argc)
        {
        case 1: // program database (comments, labels, breakpoints...)
            return Exec("dbsave");
        case 2:
            if(IsDumpFile(argv[1]))
                return Exec("minidump " + Quote(argv[1])); // whole process
            return Exec("dbsave " + Quote(argv[1])); // database to a specific file
        case 3: // the memory region (page range) that contains the address
            return Exec("savedata " + Quote(argv[1]) + ", mem.base(" + argv[2] + "), mem.size(" + argv[2] + ")");
        case 4:
            return Exec("savedata " + Quote(argv[1]) + ", " + argv[2] + ", " + argv[3]);
        default:
            _plugin_logputs(LOG_PREFIX "usage:\n"
                                       "  .save                      save the program database (comments, labels, breakpoints)\n"
                                       "  .save file.dmp             full minidump of the process\n"
                                       "  .save file.dd64            program database to this file\n"
                                       "  .save file, address        memory region containing address\n"
                                       "  .save file, address, size  memory range");
            return false;
        }
    }

    std::wstring ScyllaHideIni()
    {
        return ArchFolder() + L"\\plugins\\scylla_hide.ini";
    }

    std::wstring ScyllaHideCurrentProfile()
    {
        wchar_t profile[256] = L"";
        GetPrivateProfileStringW(L"SETTINGS", L"CurrentProfile", L"", profile, _countof(profile), ScyllaHideIni().c_str());
        return profile;
    }

    std::vector<std::wstring> ScyllaHideProfiles()
    {
        std::vector<wchar_t> buffer(32 * 1024);
        DWORD size = GetPrivateProfileSectionNamesW(buffer.data(), (DWORD)buffer.size(), ScyllaHideIni().c_str());
        std::vector<std::wstring> profiles;
        for(const wchar_t* name = buffer.data(); name < buffer.data() + size && *name; name += wcslen(name) + 1)
            if(_wcsicmp(name, L"SETTINGS") != 0)
                profiles.push_back(name);
        return profiles;
    }

    // Exact match first (case-insensitive), then a unique prefix ("vmprotect" -> "VMProtect x86/x64")
    bool FindProfile(const std::wstring & wanted, std::wstring & found)
    {
        auto profiles = ScyllaHideProfiles();
        for(auto & profile : profiles)
            if(_wcsicmp(profile.c_str(), wanted.c_str()) == 0)
                return found = profile, true;
        std::vector<std::wstring> matches;
        for(auto & profile : profiles)
            if(_wcsnicmp(profile.c_str(), wanted.c_str(), wanted.size()) == 0)
                matches.push_back(profile);
        if(matches.size() == 1)
            return found = matches[0], true;
        return false;
    }

    void LogProfiles()
    {
        std::string list;
        for(auto & profile : ScyllaHideProfiles())
            list += (list.empty() ? "" : ", ") + Utf16ToUtf8(profile);
        _plugin_logprintf(LOG_PREFIX "ScyllaHide profiles: %s\n", list.c_str());
    }

    bool ScyllaHideInstalled()
    {
        if(GetFileAttributesW(ScyllaHideIni().c_str()) != INVALID_FILE_ATTRIBUTES)
            return true;
        _plugin_logprintf(LOG_PREFIX "ScyllaHide is not installed (%s not found)\n", Utf16ToUtf8(ScyllaHideIni()).c_str());
        return false;
    }

    // ScyllaHide reads scylla_hide.ini only when it is loaded: write the profile, then reload the plugin
    bool SetScyllaHideProfile(const std::wstring & profile)
    {
        auto current = ScyllaHideCurrentProfile();
        if(_wcsicmp(current.c_str(), profile.c_str()) != 0)
        {
            if(_wcsicmp(current.c_str(), ScyllaHideDisabledProfile) != 0 && !current.empty())
                BridgeSettingSet(PLUGIN_NAME, "ScyllaHideLastProfile", Utf16ToUtf8(current).c_str());
            if(!WritePrivateProfileStringW(L"SETTINGS", L"CurrentProfile", profile.c_str(), ScyllaHideIni().c_str()))
            {
                _plugin_logprintf(LOG_PREFIX "cannot write %s (error %u)\n", Utf16ToUtf8(ScyllaHideIni()).c_str(), GetLastError());
                return false;
            }
            // plugunload + plugload: plugreload would ask for confirmation in a message box
            std::string name = ScyllaHidePluginName;
            if(!Exec("plugunload " + name) || !Exec("plugload " + name))
            {
                _plugin_logputs(LOG_PREFIX "the profile was saved but ScyllaHide could not be reloaded: restart x64dbg to apply it");
                return false;
            }
        }
        _plugin_logprintf(LOG_PREFIX "ScyllaHide profile: %s\n", Utf16ToUtf8(profile).c_str());
        if(DbgIsDebugging())
            _plugin_logputs(LOG_PREFIX "ScyllaHide applies its hooks when a process starts or is attached: "
                                       "restart the debuggee (or attach again) for the change to take effect");
        return true;
    }

    // scylla_hide.enable [profile]
    bool CbScyllaHideEnable(int argc, char* argv[])
    {
        if(!ScyllaHideInstalled())
            return false;
        std::wstring profile;
        if(argc > 1)
        {
            if(!FindProfile(Utf8ToUtf16(argv[1]), profile))
            {
                _plugin_logprintf(LOG_PREFIX "unknown ScyllaHide profile \"%s\"\n", argv[1]);
                LogProfiles();
                return false;
            }
        }
        else
        {
            auto current = ScyllaHideCurrentProfile();
            if(!current.empty() && _wcsicmp(current.c_str(), ScyllaHideDisabledProfile) != 0)
                profile = current; // already enabled
            else
            {
                char last[MAX_SETTING_SIZE] = "";
                if(!BridgeSettingGet(PLUGIN_NAME, "ScyllaHideLastProfile", last) || !FindProfile(Utf8ToUtf16(last), profile))
                    profile = ScyllaHideDefaultProfile;
            }
        }
        if(_wcsicmp(profile.c_str(), ScyllaHideDisabledProfile) == 0)
        {
            _plugin_logputs(LOG_PREFIX "use scylla_hide.disable to disable ScyllaHide");
            return false;
        }
        return SetScyllaHideProfile(profile);
    }

    bool CbScyllaHideDisable(int, char*[])
    {
        if(!ScyllaHideInstalled())
            return false;
        return SetScyllaHideProfile(ScyllaHideDisabledProfile);
    }

    bool CbScyllaHideStatus(int, char*[])
    {
        if(!ScyllaHideInstalled())
            return false;
        auto current = ScyllaHideCurrentProfile();
        bool disabled = _wcsicmp(current.c_str(), ScyllaHideDisabledProfile) == 0;
        _plugin_logprintf(LOG_PREFIX "ScyllaHide is %s, profile: %s\n", disabled ? "disabled" : "enabled", Utf16ToUtf8(current).c_str());
        LogProfiles();
        return true;
    }

    // hvkd [action [options]]: the options are passed as typed, e.g. hvkd dump --vm 0 --out C:\dumps\vm0.raw
    bool CbHvKd(int, char* argv[])
    {
        auto arguments = RawArguments(argv[0]);
        return LaunchHvKd(Utf8ToUtf16(arguments.empty() ? "menu" : arguments));
    }

    // hvkd.log action [options]
    bool CbHvKdLog(int, char* argv[])
    {
        auto arguments = RawArguments(argv[0]);
        if(arguments.empty())
        {
            _plugin_logputs(LOG_PREFIX "usage: hvkd.log action [options]   e.g. hvkd.log check, hvkd.log list, "
                                       "hvkd.log read --vm 0 --addr 0x1000 --size 0x100");
            return false;
        }
        return RunHvKdToLog(Utf8ToUtf16(arguments));
    }

    // hvkd.check [--deep]
    bool CbHvKdCheck(int, char* argv[])
    {
        auto arguments = RawArguments(argv[0]);
        return RunHvKdToLog(L"check" + (arguments.empty() ? std::wstring() : L" " + Utf8ToUtf16(arguments)));
    }

    // wineye file | wineye --live --vm 0 | wineye --from-vm 0 | ...
    bool CbWineye(int argc, char* argv[])
    {
        if(argc < 2)
        {
            _plugin_logputs(LOG_PREFIX "usage:\n"
                                       "  wineye file                     kernel crash dump (MEMORY.DMP) or raw memory image\n"
                                       "  wineye --from-vm 0              dump Hyper-V VM 0, then analyze the dump\n"
                                       "  wineye --live --vm 0            live Hyper-V VM 0 (+ freeze/resume/registers)\n"
                                       "  wineye --dump file -c \"ps;lm\"   any wineye option");
            return false;
        }
        if(argv[1][0] == '-')
            return LaunchHvKd(L"wineye " + Utf8ToUtf16(RawArguments(argv[0])));
        return OpenInWineye(argv[1]);
    }
}

extern "C" __declspec(dllexport) bool pluginit(PLUG_INITSTRUCT* initStruct)
{
    initStruct->pluginVersion = PLUGIN_VERSION;
    initStruct->sdkVersion = PLUG_SDKVERSION;
    strncpy_s(initStruct->pluginName, PLUGIN_NAME, _TRUNCATE);
    gPluginHandle = initStruct->pluginHandle;

    _plugin_registercommand(gPluginHandle, ".init", CbInit, false);
    _plugin_registercommand(gPluginHandle, ".save", CbSave, false);
    _plugin_registercommand(gPluginHandle, "scylla_hide.enable", CbScyllaHideEnable, false);
    _plugin_registercommand(gPluginHandle, "scylla_hide.disable", CbScyllaHideDisable, false);
    _plugin_registercommand(gPluginHandle, "scylla_hide.status", CbScyllaHideStatus, false);
    _plugin_registercommand(gPluginHandle, "hvkd", CbHvKd, false);
    _plugin_registercommand(gPluginHandle, "hvkd.log", CbHvKdLog, false);
    _plugin_registercommand(gPluginHandle, "hvkd.check", CbHvKdCheck, false);
    _plugin_registercommand(gPluginHandle, "wineye", CbWineye, false);
    return true;
}

extern "C" __declspec(dllexport) void plugstop()
{
}

extern "C" __declspec(dllexport) void plugsetup(PLUG_SETUPSTRUCT*)
{
}
