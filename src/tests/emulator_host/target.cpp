#include <winsock2.h>
#include <windows.h>
#include <wininet.h>
#include <shellapi.h>
#include <commctrl.h>
#include <objbase.h>
#include <oleauto.h>
#include <stdio.h>

// emulator_host.exe: the host of the emulator's child processes of the other architecture. Fathom's
// headless of that architecture starts it and saves it at its entry point (a minidump): its system
// DLLs are loaded and initialized by the system's loader, nothing of the emulated program runs
// natively. The emulator then maps the child's executable into that dump. The functions referenced
// below only make the loader load their DLLs; the program does nothing when it runs.

int main(int argc, char** argv)
{
    if(argc > 1000) // never true: references to the DLLs a child commonly needs
    {
        WSADATA wsa;
        WSAStartup(MAKEWORD(2, 2), &wsa);
        InternetOpenW(L"", 0, NULL, NULL, 0);
        MessageBoxW(NULL, L"", L"", 0);
        GetStockObject(0);
        CoInitialize(NULL);
        SysFreeString(NULL);
        ShellExecuteW(NULL, NULL, NULL, NULL, NULL, 0);
        RegCloseKey(NULL);
        InitCommonControls();
    }
    return 0;
}
