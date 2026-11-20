// Helpers shared by the commands of ExtraCmds (extracmds.cpp, emucmds.cpp)
#pragma once

#include "_plugins.h"

#include <initializer_list>
#include <string>

#define EXTRACMDS_LOG_PREFIX "[ExtraCmds] "

namespace extracmds
{
    inline std::wstring Utf8ToUtf16(const std::string & text)
    {
        if(text.empty())
            return {};
        int size = MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0);
        std::wstring result(size, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size);
        return result;
    }

    inline std::string Utf16ToUtf8(const std::wstring & text)
    {
        if(text.empty())
            return {};
        int size = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), nullptr, 0, nullptr, nullptr);
        std::string result(size, '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.c_str(), (int)text.size(), &result[0], size, nullptr, nullptr);
        return result;
    }

    // Quote an argument for an x64dbg command (same rules as Command::Escape)
    inline std::string Quote(const std::string & argument)
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

    inline bool Exec(const std::string & command)
    {
        return DbgCmdExecDirect(command.c_str());
    }

    // Folder that contains x32\ and x64\ (parent of the folder of the running executable)
    inline std::wstring X64dbgRoot()
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

    // Quote an argument for a Windows command line (CommandLineToArgvW rules)
    inline std::wstring QuoteArgument(const std::wstring & argument)
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

    inline std::wstring FullPath(const std::string & file)
    {
        auto path = Utf8ToUtf16(file);
        wchar_t full[MAX_PATH] = L"";
        if(GetFullPathNameW(path.c_str(), _countof(full), full, nullptr))
            return full;
        return path;
    }

    inline std::wstring FindPython()
    {
        wchar_t path[MAX_PATH] = L"";
        for(auto name : { L"python.exe", L"py.exe" })
            if(SearchPathW(nullptr, name, nullptr, _countof(path), path, nullptr))
                return path;
        return {};
    }

    // Run "python -X utf8 <folder>\<script> <arguments>" without a window and write its output to the
    // log line by line, as it comes. Standard input is NUL, so an interactive script stops at its
    // first prompt instead of blocking x64dbg. Returns true when the script exits with code 0.
    inline bool RunPythonToLog(const std::wstring & folder, const std::wstring & script, const std::wstring & arguments)
    {
        auto python = FindPython();
        if(python.empty())
        {
            _plugin_logputs(EXTRACMDS_LOG_PREFIX "Python not found on the PATH (python.exe or py.exe)");
            return false;
        }
        std::wstring commandLine = QuoteArgument(python) + L" -X utf8 " + QuoteArgument(folder + L"\\" + script) + L" " + arguments;
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
        SetEnvironmentVariableW(L"PYTHONUNBUFFERED", L"1");
        BOOL started = CreateProcessW(nullptr, &commandLine[0], nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, folder.c_str(), &si, &pi);
        DWORD error = GetLastError();
        SetEnvironmentVariableW(L"PYTHONUNBUFFERED", nullptr);
        CloseHandle(writePipe);
        if(nul != INVALID_HANDLE_VALUE)
            CloseHandle(nul);
        if(!started)
        {
            CloseHandle(readPipe);
            _plugin_logprintf(EXTRACMDS_LOG_PREFIX "cannot start Python (error %u)\n", error);
            return false;
        }
        auto logLine = [](std::string line)
        {
            while(!line.empty() && line.back() == '\r')
                line.pop_back();
            auto progress = line.find_last_of('\r'); // progress counters rewrite the line with \r: keep the last state
            if(progress != std::string::npos)
                line = line.substr(progress + 1);
            _plugin_logprintf("%s\n", line.c_str());
        };
        std::string pending;
        char buffer[4096];
        DWORD read = 0;
        while(ReadFile(readPipe, buffer, sizeof(buffer), &read, nullptr) && read)
        {
            pending.append(buffer, read);
            size_t start = 0;
            for(auto end = pending.find('\n'); end != std::string::npos; end = pending.find('\n', start))
            {
                logLine(pending.substr(start, end - start));
                start = end + 1;
            }
            pending.erase(0, start);
        }
        if(!pending.empty())
            logLine(pending);
        CloseHandle(readPipe);
        WaitForSingleObject(pi.hProcess, INFINITE);
        DWORD exitCode = 1;
        GetExitCodeProcess(pi.hProcess, &exitCode);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        if(exitCode != 0)
            _plugin_logprintf(EXTRACMDS_LOG_PREFIX "%s exited with code %u\n", Utf16ToUtf8(script).c_str(), exitCode);
        return exitCode == 0;
    }
}
