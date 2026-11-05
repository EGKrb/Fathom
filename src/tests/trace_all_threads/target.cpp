#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <stdio.h>

// trace_all_threads.exe: Work starts three workers that increment a shared counter with an
// interlocked instruction and call into kernel32 between two increments, then waits for them.

#define WORKERS 3
#define ITERATIONS 100

volatile LONG counter = 0;
volatile unsigned int sink = 0;
static DWORD ids[WORKERS];

static DWORD WINAPI Worker(LPVOID)
{
    for(int i = 0; i < ITERATIONS; i++)
    {
        InterlockedIncrement(&counter);
        sink += (unsigned int)GetTickCount64(); // a call into a system module
    }
    return 0;
}

extern "C" __declspec(dllexport) __declspec(noinline) void Work()
{
    HANDLE threads[WORKERS];
    for(int i = 0; i < WORKERS; i++)
        threads[i] = CreateThread(nullptr, 0, Worker, nullptr, 0, &ids[i]);
    WaitForMultipleObjects(WORKERS, threads, TRUE, INFINITE);
}

int main()
{
    // In the runtime directory (tests/trace_all_threads/): no trace of a previous run
    // (StartTraceRecording appends to an existing file), then the workers and the counter for check.py
    char path[MAX_PATH];
    DWORD length = GetModuleFileNameA(nullptr, path, MAX_PATH);
    if(length <= 4 || length >= MAX_PATH - 32)
        return 1;
    sprintf(path + length - 4, "\\trace.trace64");
    DeleteFileA(path);

    Work(); // traced

    sprintf(path + length - 4, "\\workers.txt");
    FILE* f = fopen(path, "w");
    if(f)
    {
        fprintf(f, "%p %d\n", (void*)&counter, WORKERS * ITERATIONS);
        for(int i = 0; i < WORKERS; i++)
            fprintf(f, "%lu\n", ids[i]);
        fclose(f);
    }
    return 0;
}
