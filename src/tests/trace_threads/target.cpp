#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>
#include <string.h>

// trace_threads.exe busy|wait: a worker thread spins (busy) or waits on an event (wait) while the
// main thread is traced in Work.

volatile unsigned int counter = 0;
volatile LONG spinning = 1;
static HANDLE ready, done;

static DWORD WINAPI Spinner(LPVOID)
{
    SetEvent(ready);
    while(spinning)
        counter++;
    return 0;
}

static DWORD WINAPI Waiter(LPVOID)
{
    SetEvent(ready);
    WaitForSingleObject(done, INFINITE);
    return 0;
}

extern "C" __declspec(dllexport) __declspec(noinline) void Work()
{
    for(int i = 0; i < 5000; i++)
        counter += i;
}

int main(int argc, char* argv[])
{
    const bool busyMode = argc > 1 && strcmp(argv[1], "busy") == 0;
    const char* mode = busyMode ? "busy" : "wait";
    ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    DWORD workerId = 0;
    HANDLE worker = CreateThread(nullptr, 0, busyMode ? Spinner : Waiter, nullptr, 0, &workerId);
    WaitForSingleObject(ready, INFINITE);
    Sleep(100); // the waiter is blocked in WaitForSingleObject, the spinner is spinning

    // In the runtime directory (tests/trace_threads/): the worker for check.py, and no trace of a
    // previous run (StartTraceRecording appends to an existing file)
    char path[MAX_PATH];
    DWORD length = GetModuleFileNameA(nullptr, path, MAX_PATH);
    if(length > 4 && length < MAX_PATH - 32)
    {
        sprintf(path + length - 4, "\\trace.%s.trace64", mode);
        DeleteFileA(path);
        sprintf(path + length - 4, "\\worker.%s.txt", mode);
        FILE* f = fopen(path, "w");
        if(f)
        {
            fprintf(f, "%lu\n", workerId);
            fclose(f);
        }
    }

    Work(); // traced
    spinning = 0;
    SetEvent(done);
    WaitForSingleObject(worker, INFINITE);
    return 0;
}
