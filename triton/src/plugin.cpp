// Triton plugin of Fathom: symbolic execution and taint analysis (Triton, Apache-2.0) on the debuggee
// or on a recorded Time Travel trace. The commands are in commands.cpp.
#include "_plugins.h"

#include <string>

#define PLUGIN_NAME "Triton"
#define PLUGIN_VERSION 1

namespace fathom_triton
{
    int gPluginHandle = 0;
    void RegisterCommands(int handle);
}

// libz3.dll (delay-loaded) sits next to the plugin: load it from there before Triton uses it, so that
// neither the search path of x64dbg nor another libz3 on the PATH is involved
static bool LoadZ3()
{
    HMODULE self = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCWSTR)&LoadZ3, &self);
    wchar_t path[MAX_PATH] = L"";
    GetModuleFileNameW(self, path, _countof(path));
    std::wstring dll = path;
    dll = dll.substr(0, dll.find_last_of(L'\\') + 1) + L"libz3.dll";
    HMODULE z3 = LoadLibraryExW(dll.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if(!z3)
    {
        _plugin_logprintf("[" PLUGIN_NAME "] cannot load %ls (error %lu): the sym.* commands are not available\n", dll.c_str(), GetLastError());
        return false;
    }
    return true;
}

extern "C" __declspec(dllexport) bool pluginit(PLUG_INITSTRUCT* initStruct)
{
    initStruct->pluginVersion = PLUGIN_VERSION;
    initStruct->sdkVersion = PLUG_SDKVERSION;
    strncpy_s(initStruct->pluginName, PLUGIN_NAME, _TRUNCATE);
    fathom_triton::gPluginHandle = initStruct->pluginHandle;
    if(!LoadZ3())
        return true; // loaded, without commands
    fathom_triton::RegisterCommands(initStruct->pluginHandle);
    return true;
}

extern "C" __declspec(dllexport) void plugstop()
{
}

extern "C" __declspec(dllexport) void plugsetup(PLUG_SETUPSTRUCT*)
{
}
