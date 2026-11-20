#include <windows.h>
#include <process.h>
#include <stdio.h>

// emulator_threads.exe: a whole program for the emulator test (extracmds/tests/test_emulator.py),
// run from its entry point both natively and in Fathom's emulator. Worker threads (one created
// suspended) contend for a critical section, count with interlocked instructions, wait for an event,
// a mutex and a semaphore, signal a condition variable under an SRW lock, use static TLS and sleep.
// The exit code is the number of failed checks: 0 when everything is right.

#define WORKERS 4
#define ROUNDS 3000

static CRITICAL_SECTION section;
static SRWLOCK lock = SRWLOCK_INIT;
static CONDITION_VARIABLE condition = CONDITION_VARIABLE_INIT;
static volatile LONG counter = 0;
static long total = 0;
static int finished = 0;
static int inside_semaphore = 0;
static int max_inside_semaphore = 0;
static __declspec(thread) int tls_value = 7;
static HANDLE start_event, mutex, semaphore;

static unsigned __stdcall Worker(void* parameter)
{
    int id = (int)(INT_PTR)parameter;
    if(tls_value != 7) // the static TLS of a new thread starts with its initial value
        return 100;
    tls_value = id;
    WaitForSingleObject(start_event, INFINITE);
    for(int i = 0; i < ROUNDS; i++)
    {
        EnterCriticalSection(&section);
        long value = total;
        if(i % 64 == 0)
            SwitchToThread(); // another thread tries to enter meanwhile
        total = value + id;
        LeaveCriticalSection(&section);
        InterlockedIncrement(&counter);
    }
    WaitForSingleObject(mutex, INFINITE);
    WaitForSingleObject(mutex, INFINITE); // recursive
    ReleaseMutex(mutex);
    ReleaseMutex(mutex);
    WaitForSingleObject(semaphore, INFINITE);
    EnterCriticalSection(&section);
    inside_semaphore++;
    if(inside_semaphore > max_inside_semaphore)
        max_inside_semaphore = inside_semaphore;
    LeaveCriticalSection(&section);
    Sleep(5);
    EnterCriticalSection(&section);
    inside_semaphore--;
    LeaveCriticalSection(&section);
    ReleaseSemaphore(semaphore, 1, NULL);
    AcquireSRWLockExclusive(&lock);
    finished++;
    WakeAllConditionVariable(&condition);
    ReleaseSRWLockExclusive(&lock);
    return tls_value == id ? 1000 + id : 200;
}

int main()
{
    int failures = 0;
    InitializeCriticalSection(&section);
    start_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    mutex = CreateMutexW(NULL, FALSE, NULL);
    semaphore = CreateSemaphoreW(NULL, 2, 2, NULL);
    HANDLE threads[WORKERS];
    for(int i = 0; i < WORKERS; i++)
        threads[i] = (HANDLE)_beginthreadex(NULL, 0, Worker, (void*)(INT_PTR)(i + 1), i == 0 ? CREATE_SUSPENDED : 0, NULL);
    ResumeThread(threads[0]);

    ULONGLONG before = GetTickCount64();
    Sleep(50); // the workers wait for the event meanwhile
    if(GetTickCount64() - before < 40)
        failures++, printf("Sleep(50) took %llu ms\n", GetTickCount64() - before);
    if(counter != 0)
        failures++, printf("the workers did not wait for the event\n");
    SetEvent(start_event);

    AcquireSRWLockExclusive(&lock);
    while(finished < WORKERS)
        SleepConditionVariableSRW(&condition, &lock, INFINITE, 0);
    ReleaseSRWLockExclusive(&lock);

    if(WaitForMultipleObjects(WORKERS, threads, TRUE, INFINITE) != WAIT_OBJECT_0)
        failures++, printf("WaitForMultipleObjects failed\n");
    for(int i = 0; i < WORKERS; i++)
    {
        DWORD code = 0;
        GetExitCodeThread(threads[i], &code);
        if(code != (DWORD)(1000 + i + 1))
            failures++, printf("thread %d exited with %lu\n", i + 1, code);
        CloseHandle(threads[i]);
    }
    if(counter != WORKERS * ROUNDS)
        failures++, printf("counter %ld\n", counter);
    if(total != ROUNDS * (WORKERS * (WORKERS + 1) / 2))
        failures++, printf("total %ld\n", total);
    if(max_inside_semaphore < 1 || max_inside_semaphore > 2)
        failures++, printf("semaphore let %d threads in\n", max_inside_semaphore);

    HANDLE never = CreateEventW(NULL, FALSE, FALSE, NULL);
    if(WaitForSingleObject(never, 20) != WAIT_TIMEOUT)
        failures++, printf("the wait did not time out\n");
    CloseHandle(never);

    printf("emulator_threads: %ld increments, total %ld, %d failure(s)\n", counter, total, failures);
    DeleteCriticalSection(&section);
    return failures;
}
