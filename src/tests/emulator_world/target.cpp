#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <wininet.h>
#include <shellapi.h>
#include <objbase.h>
#include <oleauto.h>
#include <msxml6.h>
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
