#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <wininet.h>
#include <shellapi.h>
#include <objbase.h>
#include <oleauto.h>
#include <msxml6.h>
#include <algorithm>
using std::max; // gdiplus.h needs min and max
using std::min;
#include <gdiplus.h>
#include <GL/gl.h>
#include <d3d9.h>
#include <d3d11.h>
#include "scene_vs.h" // compiled from scene.hlsl by fxc
#include "scene_ps.h"
#include <vector>
#include <math.h>
#include <intrin.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

// emulator_world.exe MODE: programs for the emulator test (extracmds/tests/test_emulator.py), run
// from their entry point in Fathom's emulator. The exit code is the number of failed checks.
//   race      two threads increment a counter without and with an interlocked instruction
//   illegal   an instruction the processor lacks (ud2), caught by __try / __except
//   gui       a message box, a window that paints itself (text, brush, ellipse, a button), a timer
//             and a message loop
//   net       DNS, a TCP connection and an HTTP exchange, then WinINet
//   process   a child process (this program, "child 7"), then a suspended one written to before it runs
//   pipe      a child ("upper") reads its standard input from a pipe and writes to another: the
//             parent writes, then reads the child's output until the end (both run at once)
//   shm       a section and two named events shared with a child ("shmchild"): five round trips
//   namedpipe a named pipe server and its client (a child, "pipeclient"): a message each way
//   cross     a child of the other architecture (bin\x32 <-> bin\x64), "child 5"
//   shell     ShellExecuteEx of this program ("child 6"), its process handle waited for
//   com       an in-process COM object (msxml6 DOMDocument60) parses XML; a service's status; an RPC
//   system    a file written, listed, read and deleted; a registry key written, read and deleted
//   racep     two processes increment counters in a shared section: one plainly, one with an
//             interlocked instruction (the child: "racechild")
//   winmsg    a message-only window that a child ("winmsgclient") finds, sends WM_COPYDATA to (its
//             answer: 42) and posts a registered message to
//   paint     bitmaps (a memory DC, a DIB section, StretchDIBits), regions (xor, clipping) and a
//             path drawn offscreen, checked with GetPixel, then shown in a window
//   gdiplus   GDI+ fills, a line and alpha blending into a memory DC, checked with GetPixel
//   opengl    an OpenGL 1.x context: a cleared background, a triangle, a translated square,
//             checked with glReadPixels
//   d3d9      Direct3D 9: Clear, a triangle (DrawPrimitiveUP), read back with GetRenderTargetData
//   d3d11     Direct3D 11: ClearRenderTargetView, read back through a staging texture
//   child N   exits with N

static int failures = 0;

static void Check(bool ok, const char* what)
{
    if(!ok)
    {
        failures++;
        printf("FAILED: %s\n", what);
    }
}

// ------------------------------------------------------------------ race
#define ROUNDS 200000
static volatile long racy = 0;
static volatile LONG atomic = 0;
static HANDLE go;

static DWORD WINAPI Racer(LPVOID)
{
    WaitForSingleObject(go, INFINITE);
    for(int i = 0; i < ROUNDS; i++)
    {
        racy = racy + 1;
        InterlockedIncrement(&atomic);
    }
    return 0;
}

static int Race()
{
    go = CreateEventW(NULL, TRUE, FALSE, NULL);
    HANDLE threads[2] = { CreateThread(NULL, 0, Racer, NULL, 0, NULL), CreateThread(NULL, 0, Racer, NULL, 0, NULL) };
    SetEvent(go);
    WaitForMultipleObjects(2, threads, TRUE, INFINITE);
    printf("race: racy %ld, atomic %ld of %d\n", racy, atomic, 2 * ROUNDS);
    Check(atomic == 2 * ROUNDS, "interlocked increments");
    Check(racy <= 2 * ROUNDS && racy >= ROUNDS, "plain increments in range");
    return failures;
}

// ------------------------------------------------------------------ illegal
static int Illegal()
{
    DWORD code = 0;
    __try
    {
        __ud2();
    }
    __except(code = GetExceptionCode(), EXCEPTION_EXECUTE_HANDLER)
    {
    }
    printf("illegal: exception %08lx\n", code);
    Check(code == EXCEPTION_ILLEGAL_INSTRUCTION, "ud2 raises EXCEPTION_ILLEGAL_INSTRUCTION");
    return failures;
}

// ------------------------------------------------------------------ gui
static int created = 0, timers = 0, destroyed = 0;

static LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    switch(message)
    {
    case WM_CREATE:
        created++;
        SetTimer(window, 1, 100, NULL);
        return 0;
    case WM_TIMER:
        if(++timers == 3)
        {
            KillTimer(window, 1);
            DestroyWindow(window);
        }
        return 0;
    case WM_PAINT:
    {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(window, &ps);
        RECT band = { 0, 0, 320, 60 };
        HBRUSH blue = CreateSolidBrush(RGB(0, 90, 200));
        FillRect(dc, &band, blue);
        DeleteObject(blue);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(255, 255, 255));
        TextOutW(dc, 10, 20, L"Drawn by the emulated program", 29);
        HBRUSH green = CreateSolidBrush(RGB(0, 170, 80));
        HGDIOBJ old = SelectObject(dc, green);
        Ellipse(dc, 20, 80, 120, 150);
        SelectObject(dc, old);
        DeleteObject(green);
        EndPaint(window, &ps);
        return 0;
    }
    case WM_DESTROY:
        destroyed++;
        PostQuitMessage(42);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

static int Gui()
{
    int answer = MessageBoxW(NULL, L"Hello from the emulated program", L"emulator_world", MB_OKCANCEL | MB_ICONINFORMATION);
    Check(answer == IDOK, "MessageBox answered OK");
    WNDCLASSEXW wc = { sizeof(wc) };
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"FathomEmulatorWindow";
    Check(RegisterClassExW(&wc) != 0, "RegisterClassEx");
    HWND window = CreateWindowExW(0, wc.lpszClassName, L"Emulated window", WS_OVERLAPPEDWINDOW, 0, 0, 320, 200, NULL, NULL, wc.hInstance, NULL);
    Check(window != NULL, "CreateWindowEx");
    CreateWindowExW(0, L"BUTTON", L"OK", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 200, 100, 80, 28, window, (HMENU)1, wc.hInstance, NULL);
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    ULONGLONG before = GetTickCount64();
    MSG msg;
    int quit = -1;
    while(true)
    {
        BOOL got = GetMessageW(&msg, NULL, 0, 0);
        if(got <= 0)
        {
            quit = (int)msg.wParam;
            break;
        }
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    printf("gui: created %d, timers %d, destroyed %d, quit %d, %llu ms\n", created, timers, destroyed, quit, GetTickCount64() - before);
    Check(created == 1 && timers == 3 && destroyed == 1 && quit == 42, "window, timer and message loop");
    Check(GetTickCount64() - before >= 250, "the timer follows the clock");
    return failures;
}

// ------------------------------------------------------------------ net
static int Net()
{
    WSADATA wsa;
    Check(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "WSAStartup");
    addrinfo hints = {}, *result = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    Check(getaddrinfo("example.com", "80", &hints, &result) == 0 && result != NULL, "getaddrinfo");
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    Check(s != INVALID_SOCKET, "socket");
    Check(result && connect(s, result->ai_addr, (int)result->ai_addrlen) == 0, "connect");
    const char request[] = "GET / HTTP/1.0\r\nHost: example.com\r\n\r\n";
    Check(send(s, request, (int)strlen(request), 0) == (int)strlen(request), "send");
    char response[4096] = {};
    int total = 0, n;
    while((n = recv(s, response + total, (int)sizeof(response) - 1 - total, 0)) > 0)
        total += n;
    printf("net: %d bytes received: %.15s\n", total, response);
    Check(total > 0 && strncmp(response, "HTTP/1.", 7) == 0, "an HTTP response");
    closesocket(s);
    freeaddrinfo(result);
    WSACleanup();

    HINTERNET internet = InternetOpenW(L"emulator_world", INTERNET_OPEN_TYPE_DIRECT, NULL, NULL, 0);
    Check(internet != NULL, "InternetOpen");
    HINTERNET url = internet ? InternetOpenUrlW(internet, L"http://example.com/", NULL, 0, INTERNET_FLAG_RELOAD, 0) : NULL;
    Check(url != NULL, "InternetOpenUrl");
    char page[4096] = {};
    DWORD read = 0, all = 0;
    while(url && InternetReadFile(url, page + all, (DWORD)sizeof(page) - 1 - all, &read) && read)
        all += read;
    printf("wininet: %lu bytes\n", all);
    Check(all > 0, "InternetReadFile");
    if(url)
        InternetCloseHandle(url);
    if(internet)
        InternetCloseHandle(internet);
    return failures;
}

// ------------------------------------------------------------------ process
static int Process(const wchar_t* self)
{
    wchar_t command[MAX_PATH + 16];
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi = {};
    swprintf_s(command, L"\"%s\" child 7", self);
    Check(CreateProcessW(self, command, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi) != 0, "CreateProcess");
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    printf("process: the child exited with %lu\n", code);
    Check(code == 7, "the exit code of the child");
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    // a suspended child: memory allocated and written from here, read back, then the child runs
    swprintf_s(command, L"\"%s\" child 9", self);
    Check(CreateProcessW(self, command, NULL, NULL, FALSE, CREATE_SUSPENDED, NULL, NULL, &si, &pi) != 0, "CreateProcess suspended");
    void* remote = VirtualAllocEx(pi.hProcess, NULL, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    Check(remote != NULL, "VirtualAllocEx");
    const char secret[] = "written by the parent";
    SIZE_T done = 0;
    Check(WriteProcessMemory(pi.hProcess, remote, secret, sizeof(secret), &done) && done == sizeof(secret), "WriteProcessMemory");
    char back[sizeof(secret)] = {};
    Check(ReadProcessMemory(pi.hProcess, remote, back, sizeof(back), &done) && strcmp(back, secret) == 0, "ReadProcessMemory");
    ResumeThread(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    printf("process: the suspended child exited with %lu\n", code);
    Check(code == 9, "the exit code of the suspended child");
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return failures;
}

// ------------------------------------------------------------------ helpers of the modes with a child
static PROCESS_INFORMATION Start(const wchar_t* exe, const wchar_t* arguments, BOOL inherit, STARTUPINFOW* si = NULL)
{
    wchar_t command[MAX_PATH + 64];
    swprintf_s(command, L"\"%s\" %s", exe, arguments);
    STARTUPINFOW plain = { sizeof(plain) };
    PROCESS_INFORMATION pi = {};
    if(!CreateProcessW(exe, command, NULL, NULL, inherit, 0, NULL, NULL, si ? si : &plain, &pi))
        printf("CreateProcess %ls: error %lu\n", arguments, GetLastError());
    return pi;
}

static DWORD Finish(PROCESS_INFORMATION& pi)
{
    DWORD code = 0xFFFFFFFF;
    if(pi.hProcess)
    {
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, &code);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
    return code;
}

// ------------------------------------------------------------------ pipe
static int Pipe(const wchar_t* self)
{
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    HANDLE inRead, inWrite, outRead, outWrite;
    Check(CreatePipe(&inRead, &inWrite, &sa, 0) && CreatePipe(&outRead, &outWrite, &sa, 0), "CreatePipe");
    SetHandleInformation(inWrite, HANDLE_FLAG_INHERIT, 0); // the parent's ends stay in the parent
    SetHandleInformation(outRead, HANDLE_FLAG_INHERIT, 0);
    STARTUPINFOW si = { sizeof(si) };
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = inRead;
    si.hStdOutput = outWrite;
    si.hStdError = outWrite;
    PROCESS_INFORMATION pi = Start(self, L"upper", TRUE, &si);
    CloseHandle(inRead);
    CloseHandle(outWrite);
    const char text[] = "hello from the parent";
    DWORD done = 0;
    Check(WriteFile(inWrite, text, (DWORD)strlen(text), &done, NULL) && done == strlen(text), "WriteFile to the child");
    CloseHandle(inWrite); // the end of the child's input
    char answer[256] = {};
    DWORD total = 0, read = 0;
    while(total < sizeof(answer) - 1 && ReadFile(outRead, answer + total, (DWORD)(sizeof(answer) - 1 - total), &read, NULL) && read)
        total += read;
    DWORD error = GetLastError();
    CloseHandle(outRead);
    printf("pipe: the child answered \"%s\" (then error %lu)\n", answer, error);
    Check(strcmp(answer, "HELLO FROM THE PARENT") == 0, "the child's output through the pipe");
    Check(error == ERROR_BROKEN_PIPE, "the end of the output (ERROR_BROKEN_PIPE)");
    Check(Finish(pi) == 0, "the exit code of the child");
    return failures;
}

static int Upper()
{
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE), out = GetStdHandle(STD_OUTPUT_HANDLE);
    char buffer[256];
    DWORD read = 0, written = 0;
    while(ReadFile(in, buffer, sizeof(buffer), &read, NULL) && read)
    {
        for(DWORD i = 0; i < read; i++)
            buffer[i] = (char)toupper((unsigned char)buffer[i]);
        WriteFile(out, buffer, read, &written, NULL);
    }
    return GetLastError() == ERROR_BROKEN_PIPE ? 0 : 1;
}

// ------------------------------------------------------------------ shm
#define SHM_NAME L"Local\\FathomEmulatorSection"
static int Shm(const wchar_t* self)
{
    HANDLE section = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, 4096, SHM_NAME);
    volatile LONG* shared = section ? (volatile LONG*)MapViewOfFile(section, FILE_MAP_ALL_ACCESS, 0, 0, 4096) : NULL;
    HANDLE ping = CreateEventW(NULL, FALSE, FALSE, L"Local\\FathomEmulatorPing");
    HANDLE pong = CreateEventW(NULL, FALSE, FALSE, L"Local\\FathomEmulatorPong");
    Check(shared && ping && pong, "a named section and named events");
    if(!shared || !ping || !pong)
        return failures;
    PROCESS_INFORMATION pi = Start(self, L"shmchild", FALSE);
    int rounds = 0;
    for(int i = 1; i <= 5; i++)
    {
        shared[0] = i;
        SetEvent(ping);
        if(WaitForSingleObject(pong, 10000) != WAIT_OBJECT_0)
            break;
        if(shared[1] == i * 10)
            rounds++;
    }
    printf("shm: %d round trips through the shared memory\n", rounds);
    Check(rounds == 5, "five round trips with the child");
    Check(Finish(pi) == 0, "the exit code of the child");
    return failures;
}

static int ShmChild()
{
    HANDLE section = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, SHM_NAME);
    volatile LONG* shared = section ? (volatile LONG*)MapViewOfFile(section, FILE_MAP_ALL_ACCESS, 0, 0, 4096) : NULL;
    HANDLE ping = OpenEventW(EVENT_ALL_ACCESS, FALSE, L"Local\\FathomEmulatorPing");
    HANDLE pong = OpenEventW(EVENT_ALL_ACCESS, FALSE, L"Local\\FathomEmulatorPong");
    if(!shared || !ping || !pong)
        return 1;
    for(int i = 0; i < 5; i++)
    {
        if(WaitForSingleObject(ping, 10000) != WAIT_OBJECT_0)
            return 2;
        shared[1] = shared[0] * 10;
        SetEvent(pong);
    }
    return 0;
}

// ------------------------------------------------------------------ namedpipe
#define PIPE_NAME L"\\\\.\\pipe\\fathom_emulator_world"
static int NamedPipe(const wchar_t* self)
{
    HANDLE pipe = CreateNamedPipeW(PIPE_NAME, PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT, 1, 4096, 4096, 0, NULL);
    Check(pipe != INVALID_HANDLE_VALUE, "CreateNamedPipe");
    if(pipe == INVALID_HANDLE_VALUE)
        return failures;
    PROCESS_INFORMATION pi = Start(self, L"pipeclient", FALSE);
    BOOL connected = ConnectNamedPipe(pipe, NULL) || GetLastError() == ERROR_PIPE_CONNECTED;
    Check(connected, "ConnectNamedPipe");
    char message[64] = {};
    DWORD read = 0, written = 0;
    Check(ReadFile(pipe, message, sizeof(message) - 1, &read, NULL) && strcmp(message, "ping") == 0, "the client's message");
    Check(WriteFile(pipe, "pong", 4, &written, NULL), "the answer");
    DWORD code = Finish(pi);
    printf("namedpipe: the client said \"%s\" and exited with %lu\n", message, code);
    Check(code == 0, "the client got the answer");
    CloseHandle(pipe);
    return failures;
}

static int PipeClient()
{
    HANDLE pipe = INVALID_HANDLE_VALUE;
    for(int i = 0; i < 50 && pipe == INVALID_HANDLE_VALUE; i++)
    {
        pipe = CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
        if(pipe == INVALID_HANDLE_VALUE)
            Sleep(20);
    }
    if(pipe == INVALID_HANDLE_VALUE)
        return 1;
    DWORD mode = PIPE_READMODE_MESSAGE, done = 0;
    SetNamedPipeHandleState(pipe, &mode, NULL, NULL);
    WriteFile(pipe, "ping", 4, &done, NULL);
    char answer[64] = {};
    BOOL ok = ReadFile(pipe, answer, sizeof(answer) - 1, &done, NULL);
    CloseHandle(pipe);
    return ok && strcmp(answer, "pong") == 0 ? 0 : 2;
}

// ------------------------------------------------------------------ cross
static int Cross(const wchar_t* self)
{
    // ...\bin\x64\tests\emulator_world.exe <-> ...\bin\x32\tests\emulator_world.exe
    wchar_t other[MAX_PATH];
    wcscpy_s(other, self);
    wchar_t* x64 = wcsstr(other, L"\\x64\\");
    wchar_t* x32 = wcsstr(other, L"\\x32\\");
    if(x64)
        memcpy(x64, L"\\x32\\", 5 * sizeof(wchar_t));
    else if(x32)
        memcpy(x32, L"\\x64\\", 5 * sizeof(wchar_t));
    Check(GetFileAttributesW(other) != INVALID_FILE_ATTRIBUTES, "the program of the other architecture");
    PROCESS_INFORMATION pi = Start(other, L"child 5", FALSE);
    DWORD code = Finish(pi);
    printf("cross: the child of the other architecture exited with %lu\n", code);
    Check(code == 5, "the exit code of the child of the other architecture");
    return failures;
}

// ------------------------------------------------------------------ shell
static int Shell(const wchar_t* self)
{
    SHELLEXECUTEINFOW info = { sizeof(info) };
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    info.lpVerb = L"open";
    info.lpFile = self;
    info.lpParameters = L"child 6";
    info.nShow = SW_HIDE;
    Check(ShellExecuteExW(&info) && info.hProcess != NULL, "ShellExecuteEx");
    DWORD code = 0;
    if(info.hProcess)
    {
        WaitForSingleObject(info.hProcess, INFINITE);
        GetExitCodeProcess(info.hProcess, &code);
        CloseHandle(info.hProcess);
    }
    printf("shell: the program started by ShellExecuteEx exited with %lu\n", code);
    Check(code == 6, "the exit code of the program");
    return failures;
}

// ------------------------------------------------------------------ com
static int Com()
{
    HRESULT hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    Check(SUCCEEDED(hr), "CoInitializeEx");
    IXMLDOMDocument* document = NULL;
    hr = CoCreateInstance(__uuidof(DOMDocument60), NULL, CLSCTX_INPROC_SERVER, __uuidof(IXMLDOMDocument), (void**)&document);
    Check(SUCCEEDED(hr) && document, "CoCreateInstance of DOMDocument60 (msxml6)");
    if(document)
    {
        BSTR xml = SysAllocString(L"<root><item>Fathom</item></root>");
        VARIANT_BOOL loaded = VARIANT_FALSE;
        document->loadXML(xml, &loaded);
        SysFreeString(xml);
        Check(loaded == VARIANT_TRUE, "loadXML");
        IXMLDOMElement* root = NULL;
        BSTR text = NULL;
        if(SUCCEEDED(document->get_documentElement(&root)) && root)
        {
            root->get_text(&text);
            root->Release();
        }
        printf("com: the XML document's text is \"%ls\"\n", text ? text : L"(none)");
        Check(text && wcscmp(text, L"Fathom") == 0, "the text parsed by msxml6");
        SysFreeString(text);
        document->Release();
    }
    CoUninitialize();
    // the service manager (read only) and an RPC to a service (the account of a SID: lsass)
    SC_HANDLE manager = OpenSCManagerW(NULL, NULL, SC_MANAGER_CONNECT);
    SC_HANDLE service = manager ? OpenServiceW(manager, L"EventLog", SERVICE_QUERY_STATUS) : NULL;
    SERVICE_STATUS status = {};
    Check(service && QueryServiceStatus(service, &status), "the status of a service");
    if(service)
        CloseServiceHandle(service);
    if(manager)
        CloseServiceHandle(manager);
    BYTE sid[SECURITY_MAX_SID_SIZE];
    DWORD size = sizeof(sid);
    wchar_t name[64], domain[64];
    DWORD nameSize = 64, domainSize = 64;
    SID_NAME_USE use;
    CreateWellKnownSid(WinLocalSystemSid, NULL, sid, &size);
    BOOL found = LookupAccountSidW(NULL, sid, name, &nameSize, domain, &domainSize, &use);
    printf("com: LookupAccountSid %s\n", found ? "answered" : "failed (the service did not answer)");
    return failures;
}

// ------------------------------------------------------------------ system
static int System()
{
    wchar_t folder[MAX_PATH], path[MAX_PATH];
    GetTempPathW(MAX_PATH, folder);
    wcscat_s(folder, L"fathom_emulator_world");
    CreateDirectoryW(folder, NULL);
    swprintf_s(path, L"%s\\note.txt", folder);
    HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    DWORD done = 0;
    Check(file != INVALID_HANDLE_VALUE && WriteFile(file, "written by emulator_world", 25, &done, NULL), "a file written");
    CloseHandle(file);
    wchar_t pattern[MAX_PATH];
    swprintf_s(pattern, L"%s\\*.txt", folder);
    WIN32_FIND_DATAW find;
    HANDLE search = FindFirstFileW(pattern, &find);
    Check(search != INVALID_HANDLE_VALUE && wcscmp(find.cFileName, L"note.txt") == 0 && find.nFileSizeLow == 25, "the file listed");
    if(search != INVALID_HANDLE_VALUE)
        FindClose(search);
    char back[64] = {};
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    Check(file != INVALID_HANDLE_VALUE && ReadFile(file, back, sizeof(back) - 1, &done, NULL) && strcmp(back, "written by emulator_world") == 0, "the file read back");
    CloseHandle(file);
    Check(DeleteFileW(path) && GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES, "the file deleted");
    RemoveDirectoryW(folder);

    HKEY key;
    DWORD value = 0x1234, kind = 0, size = sizeof(DWORD), read = 0;
    Check(RegCreateKeyExW(HKEY_CURRENT_USER, L"Software\\FathomEmulatorWorld", 0, NULL, 0, KEY_ALL_ACCESS, NULL, &key, NULL) == ERROR_SUCCESS, "a registry key created");
    RegSetValueExW(key, L"Value", 0, REG_DWORD, (BYTE*)&value, sizeof(value));
    Check(RegQueryValueExW(key, L"Value", NULL, &kind, (BYTE*)&read, &size) == ERROR_SUCCESS && kind == REG_DWORD && read == 0x1234, "the value read back");
    RegCloseKey(key);
    Check(RegDeleteKeyW(HKEY_CURRENT_USER, L"Software\\FathomEmulatorWorld") == ERROR_SUCCESS, "the key deleted");
    wchar_t product[128] = {};
    size = sizeof(product);
    Check(RegGetValueW(HKEY_LOCAL_MACHINE, L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", L"ProductName", RRF_RT_REG_SZ, NULL, product, &size) == ERROR_SUCCESS, "a value of the machine's registry");
    printf("system: file and registry done, the machine is \"%ls\"\n", product);
    return failures;
}

// ------------------------------------------------------------------ racep
#define RACE_ROUNDS 100000
static int RaceProcesses(const wchar_t* self)
{
    HANDLE section = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, 4096, L"Local\\FathomRaceSection");
    volatile LONG* shared = section ? (volatile LONG*)MapViewOfFile(section, FILE_MAP_ALL_ACCESS, 0, 0, 4096) : NULL;
    HANDLE go = CreateEventW(NULL, TRUE, FALSE, L"Local\\FathomRaceGo");
    Check(shared && go, "a shared section and an event");
    if(!shared || !go)
        return failures;
    shared[0] = shared[1] = 0;
    PROCESS_INFORMATION pi = Start(self, L"racechild", FALSE);
    Sleep(50);
    SetEvent(go);
    for(int i = 0; i < RACE_ROUNDS; i++)
    {
        shared[0] = shared[0] + 1;
        InterlockedIncrement(&shared[1]);
    }
    Check(Finish(pi) == 0, "the exit code of the child");
    printf("racep: racy %ld, atomic %ld of %d\n", shared[0], shared[1], 2 * RACE_ROUNDS);
    Check(shared[1] == 2 * RACE_ROUNDS, "interlocked increments of two processes");
    Check(shared[0] <= 2 * RACE_ROUNDS && shared[0] >= RACE_ROUNDS, "plain increments in range");
    return failures;
}

static int RaceChild()
{
    HANDLE section = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, L"Local\\FathomRaceSection");
    volatile LONG* shared = section ? (volatile LONG*)MapViewOfFile(section, FILE_MAP_ALL_ACCESS, 0, 0, 4096) : NULL;
    HANDLE go = OpenEventW(SYNCHRONIZE, FALSE, L"Local\\FathomRaceGo");
    if(!shared || !go || WaitForSingleObject(go, 10000) != WAIT_OBJECT_0)
        return 1;
    for(int i = 0; i < RACE_ROUNDS; i++)
    {
        shared[0] = shared[0] + 1;
        InterlockedIncrement(&shared[1]);
    }
    return 0;
}

// ------------------------------------------------------------------ winmsg
static char copied[64];
static int pings = 0;
static UINT pingMessage = 0;

static LRESULT CALLBACK ServerProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    if(message == WM_COPYDATA)
    {
        COPYDATASTRUCT* data = (COPYDATASTRUCT*)lparam;
        if(data && data->cbData < sizeof(copied))
            memcpy(copied, data->lpData, data->cbData);
        return 42;
    }
    if(message == pingMessage && pingMessage)
    {
        pings += (int)wparam;
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

static int WinMsg(const wchar_t* self)
{
    pingMessage = RegisterWindowMessageW(L"FathomEmulatorPing");
    WNDCLASSEXW wc = { sizeof(wc) };
    wc.lpfnWndProc = ServerProc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"FathomMessageServer";
    RegisterClassExW(&wc);
    HWND server = CreateWindowExW(0, wc.lpszClassName, L"server", 0, 0, 0, 0, 0, HWND_MESSAGE, NULL, wc.hInstance, NULL);
    Check(server != NULL, "a message-only window");
    PROCESS_INFORMATION pi = Start(self, L"winmsgclient", FALSE);
    MSG msg;
    SetTimer(NULL, 0, 20000, NULL); // a way out if nothing comes
    while(GetMessageW(&msg, NULL, 0, 0) > 0)
    {
        if(msg.message == WM_TIMER)
            break;
        DispatchMessageW(&msg);
    }
    DWORD code = Finish(pi);
    printf("winmsg: WM_COPYDATA \"%s\", ping %d, the client exited with %lu\n", copied, pings, code);
    Check(strcmp(copied, "hello across processes") == 0, "WM_COPYDATA from the other process");
    Check(pings == 7, "the registered message posted by the other process");
    Check(code == 0, "the client got the answer of SendMessage");
    return failures;
}

static int WinMsgClient()
{
    HWND server = NULL;
    for(int i = 0; i < 100 && !server; i++)
    {
        server = FindWindowExW(HWND_MESSAGE, NULL, L"FathomMessageServer", NULL);
        if(!server)
            Sleep(20);
    }
    if(!server)
        return 1;
    DWORD pid = 0;
    GetWindowThreadProcessId(server, &pid);
    if(pid == GetCurrentProcessId())
        return 2;
    const char text[] = "hello across processes";
    COPYDATASTRUCT data = { 1, sizeof(text), (void*)text };
    LRESULT answer = SendMessageW(server, WM_COPYDATA, 0, (LPARAM)&data);
    PostMessageW(server, RegisterWindowMessageW(L"FathomEmulatorPing"), 7, 0);
    return answer == 42 ? 0 : 3;
}

// ------------------------------------------------------------------ paint
static HDC offscreen;
static int painted = 0;

static void CheckPixel(int x, int y, COLORREF expected, const char* what)
{
    COLORREF got = GetPixel(offscreen, x, y);
    if(got != expected)
        printf("paint: %s at (%d, %d): %06lx instead of %06lx\n", what, x, y, got, expected);
    Check(got == expected, what);
}

static LRESULT CALLBACK PaintProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam)
{
    switch(message)
    {
    case WM_CREATE:
        SetTimer(window, 1, 100, NULL);
        return 0;
    case WM_PAINT:
    {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(window, &ps);
        BitBlt(dc, 0, 0, 300, 200, offscreen, 0, 0, SRCCOPY);
        painted++;
        EndPaint(window, &ps);
        return 0;
    }
    case WM_TIMER:
        KillTimer(window, 1);
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

static int Paint()
{
    HDC screen = GetDC(NULL);
    offscreen = CreateCompatibleDC(screen);
    HBITMAP canvas = CreateCompatibleBitmap(screen, 300, 200);
    SelectObject(offscreen, canvas);
    RECT all = { 0, 0, 300, 200 };
    FillRect(offscreen, &all, (HBRUSH)GetStockObject(WHITE_BRUSH));

    // a memory DC with a bitmap, copied with BitBlt
    HDC memory = CreateCompatibleDC(screen);
    HBITMAP red = CreateCompatibleBitmap(screen, 60, 40);
    SelectObject(memory, red);
    RECT redArea = { 0, 0, 60, 40 };
    HBRUSH redBrush = CreateSolidBrush(RGB(255, 0, 0));
    FillRect(memory, &redArea, redBrush);
    BitBlt(offscreen, 10, 10, 60, 40, memory, 0, 0, SRCCOPY);

    // a DIB section filled through its pixels
    BITMAPINFO info = {};
    info.bmiHeader.biSize = sizeof(info.bmiHeader);
    info.bmiHeader.biWidth = 40;
    info.bmiHeader.biHeight = -30;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    void* bits = NULL;
    HBITMAP dib = CreateDIBSection(screen, &info, DIB_RGB_COLORS, &bits, NULL, 0);
    if(bits)
        for(int i = 0; i < 40 * 30; i++)
            ((DWORD*)bits)[i] = 0x00FFFF00; // yellow (BGRX)
    GdiFlush();
    SelectObject(memory, dib);
    BitBlt(offscreen, 80, 10, 40, 30, memory, 0, 0, SRCCOPY);

    // StretchDIBits of a 2x2 cyan image to 40x40
    DWORD cyan[4] = { 0x0000FFFF, 0x0000FFFF, 0x0000FFFF, 0x0000FFFF };
    info.bmiHeader.biWidth = 2;
    info.bmiHeader.biHeight = 2;
    StretchDIBits(offscreen, 130, 10, 40, 40, 0, 0, 2, 2, cyan, &info, DIB_RGB_COLORS, SRCCOPY);

    // regions: an ellipse xor a rectangle, filled in blue
    HRGN ellipse = CreateEllipticRgn(10, 60, 70, 120);
    HRGN rectangle = CreateRectRgn(40, 60, 100, 120);
    HRGN both = CreateRectRgn(0, 0, 1, 1);
    CombineRgn(both, ellipse, rectangle, RGN_XOR);
    HBRUSH blue = CreateSolidBrush(RGB(0, 0, 255));
    FillRgn(offscreen, both, blue);

    // a path: a triangle filled in green
    HBRUSH green = CreateSolidBrush(RGB(0, 160, 0));
    HGDIOBJ oldBrush = SelectObject(offscreen, green);
    BeginPath(offscreen);
    MoveToEx(offscreen, 130, 60, NULL);
    LineTo(offscreen, 190, 60);
    LineTo(offscreen, 160, 120);
    CloseFigure(offscreen);
    EndPath(offscreen);
    FillPath(offscreen);
    SelectObject(offscreen, oldBrush);

    // clipping: a magenta fill limited to a rectangle
    HRGN clip = CreateRectRgn(200, 60, 240, 100);
    SelectClipRgn(offscreen, clip);
    RECT big = { 200, 60, 280, 140 };
    HBRUSH magenta = CreateSolidBrush(RGB(255, 0, 255));
    FillRect(offscreen, &big, magenta);
    SelectClipRgn(offscreen, NULL);

    CheckPixel(20, 20, RGB(255, 0, 0), "BitBlt of a memory bitmap");
    CheckPixel(90, 20, RGB(255, 255, 0), "a DIB section written through its pixels");
    CheckPixel(140, 20, RGB(0, 255, 255), "StretchDIBits");
    CheckPixel(20, 90, RGB(0, 0, 255), "a region: the ellipse alone");
    CheckPixel(55, 90, RGB(255, 255, 255), "a region: ellipse xor rectangle, outside");
    CheckPixel(95, 65, RGB(0, 0, 255), "a region: the rectangle alone");
    CheckPixel(160, 75, RGB(0, 160, 0), "a filled path");
    CheckPixel(185, 115, RGB(255, 255, 255), "outside of the path");
    CheckPixel(220, 80, RGB(255, 0, 255), "inside of the clipping region");
    CheckPixel(260, 120, RGB(255, 255, 255), "outside of the clipping region");

    // AlphaBlend with an alpha per pixel (premultiplied): half transparent blue over white
    BITMAPINFO alphaInfo = {};
    alphaInfo.bmiHeader.biSize = sizeof(alphaInfo.bmiHeader);
    alphaInfo.bmiHeader.biWidth = 20;
    alphaInfo.bmiHeader.biHeight = -20;
    alphaInfo.bmiHeader.biPlanes = 1;
    alphaInfo.bmiHeader.biBitCount = 32;
    void* alphaBits = NULL;
    HBITMAP alphaDib = CreateDIBSection(screen, &alphaInfo, DIB_RGB_COLORS, &alphaBits, NULL, 0);
    if(alphaBits)
        for(int i = 0; i < 20 * 20; i++)
            ((DWORD*)alphaBits)[i] = 0x80000080; // alpha 0x80, blue 0x80 (premultiplied)
    GdiFlush();
    SelectObject(memory, alphaDib);
    BLENDFUNCTION blend = { AC_SRC_OVER, 0, 255, AC_SRC_ALPHA };
    AlphaBlend(offscreen, 250, 10, 20, 20, memory, 0, 0, 20, 20, blend);
    COLORREF mixed = GetPixel(offscreen, 260, 20);
    bool blended = abs((int)GetRValue(mixed) - 127) <= 2 && abs((int)GetGValue(mixed) - 127) <= 2 && abs((int)GetBValue(mixed) - 255) <= 2;
    if(!blended)
        printf("paint: AlphaBlend with an alpha per pixel: %06lx\n", mixed);
    Check(blended, "AlphaBlend with an alpha per pixel");

    WNDCLASSEXW wc = { sizeof(wc) };
    wc.lpfnWndProc = PaintProc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"FathomPaintWindow";
    RegisterClassExW(&wc);
    HWND window = CreateWindowExW(0, wc.lpszClassName, L"Bitmaps regions paths", WS_OVERLAPPED | WS_CAPTION, 0, 0, 300, 200, NULL, NULL, wc.hInstance, NULL);
    ShowWindow(window, SW_SHOWNOACTIVATE);
    UpdateWindow(window);
    MSG msg;
    while(GetMessageW(&msg, NULL, 0, 0) > 0)
        DispatchMessageW(&msg);
    printf("paint: %d pixel checks failed, painted %d time(s)\n", failures, painted);
    return failures;
}

// ------------------------------------------------------------------ gdiplus, opengl, d3d9, d3d11
static bool Near(unsigned value, unsigned expected)
{
    return (int)value - (int)expected <= 2 && (int)expected - (int)value <= 2;
}

static int GdiPlus()
{
    Gdiplus::GdiplusStartupInput input;
    ULONG_PTR token = 0;
    Check(Gdiplus::GdiplusStartup(&token, &input, NULL) == Gdiplus::Ok, "GdiplusStartup");
    HDC screen = GetDC(NULL);
    offscreen = CreateCompatibleDC(screen);
    HBITMAP canvas = CreateCompatibleBitmap(screen, 200, 100);
    SelectObject(offscreen, canvas);
    RECT all = { 0, 0, 200, 100 };
    FillRect(offscreen, &all, (HBRUSH)GetStockObject(WHITE_BRUSH));
    {
        Gdiplus::Graphics graphics(offscreen);
        Gdiplus::SolidBrush red(Gdiplus::Color(255, 255, 0, 0));
        Check(graphics.FillRectangle(&red, 10, 10, 60, 40) == Gdiplus::Ok, "GDI+ FillRectangle");
        Gdiplus::SolidBrush green(Gdiplus::Color(255, 0, 160, 0));
        Check(graphics.FillEllipse(&green, 100, 10, 60, 60) == Gdiplus::Ok, "GDI+ FillEllipse");
        Gdiplus::Pen blue(Gdiplus::Color(255, 0, 0, 255), 3);
        Check(graphics.DrawLine(&blue, 10, 85, 190, 85) == Gdiplus::Ok, "GDI+ DrawLine");
        Gdiplus::SolidBrush halfRed(Gdiplus::Color(128, 255, 0, 0));
        graphics.FillRectangle(&halfRed, 170, 10, 20, 20);
    }
    CheckPixel(30, 30, RGB(255, 0, 0), "GDI+ rectangle");
    CheckPixel(130, 40, RGB(0, 160, 0), "GDI+ ellipse");
    CheckPixel(100, 85, RGB(0, 0, 255), "GDI+ line");
    CheckPixel(5, 5, RGB(255, 255, 255), "outside of the GDI+ shapes");
    COLORREF mixed = GetPixel(offscreen, 180, 20);
    Check(Near(GetRValue(mixed), 255) && Near(GetGValue(mixed), 127) && Near(GetBValue(mixed), 127), "GDI+ alpha blending");
    Gdiplus::GdiplusShutdown(token);
    printf("gdiplus: %d check(s) failed\n", failures);
    return failures;
}

static HWND TestWindow(const wchar_t* title)
{
    WNDCLASSEXW wc = { sizeof(wc) };
    wc.style = CS_OWNDC;
    wc.lpfnWndProc = DefWindowProcW;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"FathomGraphicsWindow";
    RegisterClassExW(&wc);
    HWND window = CreateWindowExW(WS_EX_TOPMOST, wc.lpszClassName, title, WS_POPUP | WS_VISIBLE, 0, 0, 200, 200, NULL, NULL, wc.hInstance, NULL);
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    return window;
}

static int OpenGl()
{
    HWND window = TestWindow(L"OpenGL");
    HDC dc = GetDC(window);
    PIXELFORMATDESCRIPTOR pfd = { sizeof(pfd), 1, PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER, PFD_TYPE_RGBA, 32 };
    pfd.cDepthBits = 24;
    int format = ChoosePixelFormat(dc, &pfd);
    Check(format != 0 && SetPixelFormat(dc, format, &pfd), "a pixel format");
    HGLRC context = wglCreateContext(dc);
    Check(context && wglMakeCurrent(dc, context), "an OpenGL context");
    glViewport(0, 0, 200, 200);
    glClearColor(0.0f, 0.0f, 0.5f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    glOrtho(-1, 1, -1, 1, -1, 1);
    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();
    glBegin(GL_TRIANGLES);
    glColor3f(1.0f, 0.5f, 0.0f);
    glVertex2f(-0.5f, -0.5f);
    glVertex2f(0.5f, -0.5f);
    glVertex2f(0.0f, 0.5f);
    glEnd();
    glTranslatef(0.75f, 0.75f, 0.0f);
    glBegin(GL_QUADS);
    glColor3ub(0, 200, 0);
    glVertex2f(-0.1f, -0.1f);
    glVertex2f(0.1f, -0.1f);
    glVertex2f(0.1f, 0.1f);
    glVertex2f(-0.1f, 0.1f);
    glEnd();
    // a textured square in the top left corner: 2x2 texels, nearest
    glLoadIdentity();
    const GLubyte texels[16] = { 255, 255, 255, 255, 255, 0, 0, 255, 0, 0, 255, 255, 0, 255, 0, 255 }; // white red / blue green
    GLuint texture = 0;
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 2, 2, 0, GL_RGBA, GL_UNSIGNED_BYTE, texels);
    glEnable(GL_TEXTURE_2D);
    glColor3f(1.0f, 1.0f, 1.0f);
    glBegin(GL_QUADS);
    glTexCoord2f(0, 0);
    glVertex2f(-1.0f, 0.6f);
    glTexCoord2f(1, 0);
    glVertex2f(-0.6f, 0.6f);
    glTexCoord2f(1, 1);
    glVertex2f(-0.6f, 1.0f);
    glTexCoord2f(0, 1);
    glVertex2f(-1.0f, 1.0f);
    glEnd();
    glDisable(GL_TEXTURE_2D);
    glFinish();
    GLubyte texel[4][4] = {};
    const int texelAt[4][2] = { { 10, 170 }, { 30, 170 }, { 10, 190 }, { 30, 190 } };
    for(int i = 0; i < 4; i++)
    {
        glReadPixels(texelAt[i][0], texelAt[i][1], 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, texel[i]);
        Check(Near(texel[i][0], texels[4 * i]) && Near(texel[i][1], texels[4 * i + 1]) && Near(texel[i][2], texels[4 * i + 2]),
              "a texel of the OpenGL texture");
    }
    glDeleteTextures(1, &texture);
    GLubyte center[4] = {}, corner[4] = {}, square[4] = {};
    glReadPixels(100, 80, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, center);
    glReadPixels(10, 10, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, corner);
    glReadPixels(175, 175, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, square);
    printf("opengl: %s, triangle %u %u %u, background %u %u %u, square %u %u %u\n", (const char*)glGetString(GL_VERSION),
           center[0], center[1], center[2], corner[0], corner[1], corner[2], square[0], square[1], square[2]);
    Check(Near(center[0], 255) && Near(center[1], 128) && Near(center[2], 0), "the OpenGL triangle");
    Check(Near(corner[0], 0) && Near(corner[1], 0) && Near(corner[2], 128), "the OpenGL background");
    Check(Near(square[0], 0) && Near(square[1], 200) && Near(square[2], 0), "the translated OpenGL square");
    SwapBuffers(dc);
    wglMakeCurrent(NULL, NULL);
    wglDeleteContext(context);
    ReleaseDC(window, dc);
    DestroyWindow(window);
    return failures;
}

static int D3D9()
{
    HWND window = TestWindow(L"Direct3D 9");
    IDirect3D9* d3d = Direct3DCreate9(D3D_SDK_VERSION);
    Check(d3d != NULL, "Direct3DCreate9");
    if(!d3d)
        return failures;
    D3DPRESENT_PARAMETERS pp = {};
    pp.Windowed = TRUE;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferWidth = 200;
    pp.BackBufferHeight = 200;
    pp.hDeviceWindow = window;
    IDirect3DDevice9* device = NULL;
    HRESULT hr = d3d->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, window, D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &device);
    Check(SUCCEEDED(hr) && device, "CreateDevice");
    if(!device)
        return failures;
    device->Clear(0, NULL, D3DCLEAR_TARGET, D3DCOLOR_XRGB(0, 0, 128), 1.0f, 0);
    device->BeginScene();
    struct Vertex
    {
        float x, y, z, rhw;
        DWORD color;
    } triangle[3] = { { 50, 150, 0, 1, 0xFFFF8000 }, { 100, 50, 0, 1, 0xFFFF8000 }, { 150, 150, 0, 1, 0xFFFF8000 } }; // clockwise: not culled
    device->SetFVF(D3DFVF_XYZRHW | D3DFVF_DIFFUSE);
    Check(SUCCEEDED(device->DrawPrimitiveUP(D3DPT_TRIANGLELIST, 1, triangle, sizeof(Vertex))), "DrawPrimitiveUP");
    // a textured square in the top right corner: 2x2 texels (white red / blue green), point sampling
    const DWORD texels[4] = { 0xFFFFFFFF, 0xFFFF0000, 0xFF0000FF, 0xFF00FF00 };
    IDirect3DTexture9* texture = NULL;
    if(SUCCEEDED(device->CreateTexture(2, 2, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &texture, NULL)))
    {
        D3DLOCKED_RECT locked;
        if(SUCCEEDED(texture->LockRect(0, &locked, NULL, 0)))
        {
            for(int y = 0; y < 2; y++)
                memcpy((BYTE*)locked.pBits + y * locked.Pitch, texels + 2 * y, 8);
            texture->UnlockRect(0);
        }
        struct TexturedVertex
        {
            float x, y, z, rhw;
            DWORD color;
            float u, v;
        } square[4] = { { 160, 0, 0, 1, 0xFFFFFFFF, 0, 0 }, { 200, 0, 0, 1, 0xFFFFFFFF, 1, 0 },
                        { 160, 40, 0, 1, 0xFFFFFFFF, 0, 1 }, { 200, 40, 0, 1, 0xFFFFFFFF, 1, 1 } };
        device->SetTexture(0, texture);
        device->SetFVF(D3DFVF_XYZRHW | D3DFVF_DIFFUSE | D3DFVF_TEX1);
        Check(SUCCEEDED(device->DrawPrimitiveUP(D3DPT_TRIANGLESTRIP, 2, square, sizeof(TexturedVertex))), "a textured DrawPrimitiveUP");
        device->SetTexture(0, NULL);
    }
    device->EndScene();
    IDirect3DSurface9* back = NULL;
    IDirect3DSurface9* copy = NULL;
    device->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &back);
    device->CreateOffscreenPlainSurface(200, 200, D3DFMT_X8R8G8B8, D3DPOOL_SYSTEMMEM, &copy, NULL);
    DWORD center = 0, corner = 0;
    if(back && copy && SUCCEEDED(device->GetRenderTargetData(back, copy)))
    {
        D3DLOCKED_RECT locked;
        if(SUCCEEDED(copy->LockRect(&locked, NULL, D3DLOCK_READONLY)))
        {
            center = ((DWORD*)((BYTE*)locked.pBits + 120 * locked.Pitch))[100] & 0xFFFFFF;
            corner = ((DWORD*)((BYTE*)locked.pBits + 10 * locked.Pitch))[10] & 0xFFFFFF;
            const int texelAt[4][2] = { { 170, 10 }, { 190, 10 }, { 170, 30 }, { 190, 30 } };
            for(int i = 0; i < 4; i++)
            {
                DWORD value = ((DWORD*)((BYTE*)locked.pBits + texelAt[i][1] * locked.Pitch))[texelAt[i][0]] & 0xFFFFFF;
                if(value != (texels[i] & 0xFFFFFF))
                    printf("d3d9: texel %d: %06lx\n", i, value);
                Check(value == (texels[i] & 0xFFFFFF), "a texel of the Direct3D 9 texture");
            }
            copy->UnlockRect();
        }
    }
    printf("d3d9: triangle %06lx, background %06lx\n", center, corner);
    Check(center == 0xFF8000, "the Direct3D 9 triangle");
    Check(corner == 0x000080, "the Direct3D 9 background");
    device->Present(NULL, NULL, NULL, NULL);
    if(texture)
        texture->Release();
    if(copy)
        copy->Release();
    if(back)
        back->Release();
    device->Release();
    d3d->Release();
    DestroyWindow(window);
    return failures;
}

static int D3D11()
{
    HWND window = TestWindow(L"Direct3D 11");
    DXGI_SWAP_CHAIN_DESC sd = {};
    sd.BufferCount = 1;
    sd.BufferDesc.Width = 200;
    sd.BufferDesc.Height = 200;
    sd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = window;
    sd.SampleDesc.Count = 1;
    sd.Windowed = TRUE;
    IDXGISwapChain* chain = NULL;
    ID3D11Device* device = NULL;
    ID3D11DeviceContext* context = NULL;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0, D3D11_SDK_VERSION, &sd, &chain, &device, NULL, &context);
    Check(SUCCEEDED(hr) && chain && device && context, "D3D11CreateDeviceAndSwapChain");
    if(!chain || !device || !context)
        return failures;
    ID3D11Texture2D* texture = NULL;
    chain->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&texture);
    ID3D11RenderTargetView* view = NULL;
    device->CreateRenderTargetView(texture, NULL, &view);
    float green[4] = { 0.0f, 0.6f, 0.0f, 1.0f };
    context->ClearRenderTargetView(view, green);

    // a scene drawn by shaders (scene.hlsl): an orange triangle, a textured square in the top right corner
    struct SceneVertex
    {
        float x, y;
        float r, g, b, a;
        float u, v;
    } vertices[9] = {
        { -0.5f, -0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f }, { 0.0f, 0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f }, { 0.5f, -0.5f, 1, 0.5f, 0, 1, 0.25f, 0.25f },
        { 0.6f, 1.0f, 1, 1, 1, 1, 0, 0 }, { 1.0f, 1.0f, 1, 1, 1, 1, 1, 0 }, { 1.0f, 0.6f, 1, 1, 1, 1, 1, 1 },
        { 0.6f, 1.0f, 1, 1, 1, 1, 0, 0 }, { 1.0f, 0.6f, 1, 1, 1, 1, 1, 1 }, { 0.6f, 0.6f, 1, 1, 1, 1, 0, 1 },
    };
    D3D11_BUFFER_DESC bd = { sizeof(vertices), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER };
    D3D11_SUBRESOURCE_DATA initial = { vertices };
    ID3D11Buffer* vertexBuffer = NULL;
    device->CreateBuffer(&bd, &initial, &vertexBuffer);
    float tint[4] = { 1, 1, 1, 1 };
    D3D11_BUFFER_DESC cd = { sizeof(tint), D3D11_USAGE_DYNAMIC, D3D11_BIND_CONSTANT_BUFFER, D3D11_CPU_ACCESS_WRITE };
    ID3D11Buffer* constants = NULL;
    device->CreateBuffer(&cd, NULL, &constants);
    D3D11_MAPPED_SUBRESOURCE written;
    if(constants && SUCCEEDED(context->Map(constants, 0, D3D11_MAP_WRITE_DISCARD, 0, &written)))
    {
        memcpy(written.pData, tint, sizeof(tint));
        context->Unmap(constants, 0);
    }
    const UINT texels[4] = { 0xFFFFFFFF, 0xFF0000FF, 0xFFFF0000, 0xFF00FF00 }; // R8G8B8A8: white red / blue green
    D3D11_TEXTURE2D_DESC td = { 2, 2, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D11_USAGE_DEFAULT, D3D11_BIND_SHADER_RESOURCE };
    D3D11_SUBRESOURCE_DATA pixels = { texels, 8 };
    ID3D11Texture2D* picture = NULL;
    ID3D11ShaderResourceView* pictureView = NULL;
    device->CreateTexture2D(&td, &pixels, &picture);
    if(picture)
        device->CreateShaderResourceView(picture, NULL, &pictureView);
    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    ID3D11SamplerState* nearest = NULL;
    device->CreateSamplerState(&sampler, &nearest);
    ID3D11VertexShader* vs = NULL;
    ID3D11PixelShader* ps = NULL;
    ID3D11InputLayout* layout = NULL;
    device->CreateVertexShader(SceneVertexShader, sizeof(SceneVertexShader), NULL, &vs);
    device->CreatePixelShader(ScenePixelShader, sizeof(ScenePixelShader), NULL, &ps);
    D3D11_INPUT_ELEMENT_DESC elements[3] = {
        { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, D3D11_APPEND_ALIGNED_ELEMENT, D3D11_INPUT_PER_VERTEX_DATA, 0 },
        { "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D11_APPEND_ALIGNED_ELEMENT, D3D11_INPUT_PER_VERTEX_DATA, 0 },
    };
    device->CreateInputLayout(elements, 3, SceneVertexShader, sizeof(SceneVertexShader), &layout);
    Check(vertexBuffer && constants && pictureView && nearest && vs && ps && layout, "the objects of the Direct3D 11 scene");
    if(vertexBuffer && constants && pictureView && nearest && vs && ps && layout)
    {
        UINT stride = sizeof(SceneVertex), offset = 0;
        D3D11_VIEWPORT viewport = { 0, 0, 200, 200, 0, 1 };
        context->OMSetRenderTargets(1, &view, NULL);
        context->RSSetViewports(1, &viewport);
        context->IASetInputLayout(layout);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context->IASetVertexBuffers(0, 1, &vertexBuffer, &stride, &offset);
        context->VSSetShader(vs, NULL, 0);
        context->VSSetConstantBuffers(0, 1, &constants);
        context->PSSetShader(ps, NULL, 0);
        context->PSSetShaderResources(0, 1, &pictureView);
        context->PSSetSamplers(0, 1, &nearest);
        context->Draw(9, 0);
    }
    D3D11_TEXTURE2D_DESC desc;
    texture->GetDesc(&desc);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    ID3D11Texture2D* staging = NULL;
    device->CreateTexture2D(&desc, NULL, &staging);
    BYTE pixel[4] = {}, center[4] = {}, texel[4][4] = {};
    const int texelAt[4][2] = { { 170, 10 }, { 190, 10 }, { 170, 30 }, { 190, 30 } };
    if(staging)
    {
        context->CopyResource(staging, texture);
        D3D11_MAPPED_SUBRESOURCE mapped;
        if(SUCCEEDED(context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped)))
        {
            memcpy(pixel, (BYTE*)mapped.pData + 190 * mapped.RowPitch + 10 * 4, 4);
            memcpy(center, (BYTE*)mapped.pData + 120 * mapped.RowPitch + 100 * 4, 4);
            for(int i = 0; i < 4; i++)
                memcpy(texel[i], (BYTE*)mapped.pData + texelAt[i][1] * mapped.RowPitch + texelAt[i][0] * 4, 4);
            context->Unmap(staging, 0);
        }
        staging->Release();
    }
    printf("d3d11: cleared to %u %u %u, triangle %u %u %u\n", pixel[0], pixel[1], pixel[2], center[0], center[1], center[2]);
    Check(Near(pixel[0], 0) && Near(pixel[1], 153) && Near(pixel[2], 0), "ClearRenderTargetView");
    Check(Near(center[0], 255) && Near(center[1], 128) && Near(center[2], 0), "the triangle drawn by the Direct3D 11 shaders");
    for(int i = 0; i < 4; i++)
    {
        const BYTE* expected = (const BYTE*)&texels[i];
        if(!(Near(texel[i][0], expected[0]) && Near(texel[i][1], expected[1]) && Near(texel[i][2], expected[2])))
            printf("d3d11: texel %d: %u %u %u\n", i, texel[i][0], texel[i][1], texel[i][2]);
        Check(Near(texel[i][0], expected[0]) && Near(texel[i][1], expected[1]) && Near(texel[i][2], expected[2]),
              "a texel sampled by the Direct3D 11 pixel shader");
    }
    if(layout)
        layout->Release();
    if(ps)
        ps->Release();
    if(vs)
        vs->Release();
    if(nearest)
        nearest->Release();
    if(pictureView)
        pictureView->Release();
    if(picture)
        picture->Release();
    if(constants)
        constants->Release();
    if(vertexBuffer)
        vertexBuffer->Release();
    chain->Present(0, 0);
    view->Release();
    texture->Release();
    context->Release();
    device->Release();
    chain->Release();
    DestroyWindow(window);
    return failures;
}

#include "graphics_more.inc"
#include "graphics_more2.inc"
#include "graphics_more3.inc"
#include "graphics_more4.inc"

int wmain(int argc, wchar_t** argv)
{
    if(argc < 2)
        return 100;
    wchar_t self[MAX_PATH];
    GetModuleFileNameW(NULL, self, MAX_PATH);
    if(wcscmp(argv[1], L"pipe") == 0)
        return Pipe(self);
    if(wcscmp(argv[1], L"upper") == 0)
        return Upper();
    if(wcscmp(argv[1], L"shm") == 0)
        return Shm(self);
    if(wcscmp(argv[1], L"shmchild") == 0)
        return ShmChild();
    if(wcscmp(argv[1], L"namedpipe") == 0)
        return NamedPipe(self);
    if(wcscmp(argv[1], L"pipeclient") == 0)
        return PipeClient();
    if(wcscmp(argv[1], L"cross") == 0)
        return Cross(self);
    if(wcscmp(argv[1], L"shell") == 0)
        return Shell(self);
    if(wcscmp(argv[1], L"com") == 0)
        return Com();
    if(wcscmp(argv[1], L"system") == 0)
        return System();
    if(wcscmp(argv[1], L"racep") == 0)
        return RaceProcesses(self);
    if(wcscmp(argv[1], L"racechild") == 0)
        return RaceChild();
    if(wcscmp(argv[1], L"winmsg") == 0)
        return WinMsg(self);
    if(wcscmp(argv[1], L"winmsgclient") == 0)
        return WinMsgClient();
    if(wcscmp(argv[1], L"paint") == 0)
        return Paint();
    if(wcscmp(argv[1], L"gdiplus") == 0)
        return GdiPlus();
    if(wcscmp(argv[1], L"opengl") == 0)
        return OpenGl();
    if(wcscmp(argv[1], L"d3d9") == 0)
        return D3D9();
    if(wcscmp(argv[1], L"d3d11") == 0)
        return D3D11();
    if(wcscmp(argv[1], L"clip") == 0)
        return GlClip();
    if(wcscmp(argv[1], L"gl3") == 0)
        return Gl3();
    if(wcscmp(argv[1], L"d3d9sh") == 0)
        return D3D9Shaders();
    if(wcscmp(argv[1], L"d3d10") == 0)
        return D3D10();
    if(wcscmp(argv[1], L"d3d11gs") == 0)
        return D3D11Stages();
    if(wcscmp(argv[1], L"d3d11cs") == 0)
        return D3D11Compute();
    if(wcscmp(argv[1], L"d3d12") == 0)
        return D3D12();
    if(wcscmp(argv[1], L"mesh") == 0)
        return Mesh();
    if(wcscmp(argv[1], L"bc") == 0)
        return Bc();
    if(wcscmp(argv[1], L"deriv") == 0)
        return Deriv();
    if(wcscmp(argv[1], L"tess") == 0)
        return Tess();
    if(wcscmp(argv[1], L"so") == 0)
        return StreamOut();
    if(wcscmp(argv[1], L"deferred") == 0)
        return Deferred();
    if(wcscmp(argv[1], L"gl4") == 0)
        return Gl4();
    if(wcscmp(argv[1], L"fx10") == 0)
        return Fx10();
    if(wcscmp(argv[1], L"dxil") == 0)
        return Dxil();
    if(wcscmp(argv[1], L"dxr") == 0)
        return Dxr();
    if(wcscmp(argv[1], L"dxilgs") == 0)
        return DxilStages();
    if(wcscmp(argv[1], L"dxilops") == 0)
        return DxilOps();
    if(wcscmp(argv[1], L"dxr2") == 0)
        return Dxr2();
    if(wcscmp(argv[1], L"mesh6") == 0)
        return Mesh6();
    if(wcscmp(argv[1], L"graph") == 0)
        return Graph();
    if(wcscmp(argv[1], L"sm69") == 0)
        return Sm69();
    if(wcscmp(argv[1], L"race") == 0)
        return Race();
    if(wcscmp(argv[1], L"illegal") == 0)
        return Illegal();
    if(wcscmp(argv[1], L"gui") == 0)
        return Gui();
    if(wcscmp(argv[1], L"net") == 0)
        return Net();
    if(wcscmp(argv[1], L"process") == 0)
        return Process(self);
    if(wcscmp(argv[1], L"child") == 0 && argc > 2)
    {
        printf("child: exiting with %ls\n", argv[2]);
        return _wtoi(argv[2]);
    }
    return 101;
}
