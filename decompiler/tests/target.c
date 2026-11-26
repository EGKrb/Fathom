// Test program of the Decompiler plugin (tests/test_decompiler.py): functions whose decompiled C the test
// checks (a loop, a switch, calls to the Windows API through the import table).
#include <windows.h>
#include <stdio.h>

__declspec(dllexport) __declspec(noinline) unsigned checksum(const unsigned char* data, unsigned length)
{
    unsigned sum = 0x1505;
    for(unsigned i = 0; i < length; i++)
        sum = sum * 33 + data[i];
    return sum;
}

__declspec(dllexport) __declspec(noinline) const char* describe(int code)
{
    switch(code)
    {
    case 0: return "zero";
    case 1: return "one";
    case 2: return "two";
    case 3: return "three";
    case 4: return "four";
    case 5: return "five";
    default: return "many";
    }
}

__declspec(dllexport) __declspec(noinline) DWORD greet(const char* name)
{
    char text[64];
    wsprintfA(text, "hello %s", name);
    OutputDebugStringA(text);
    return GetCurrentProcessId() ^ lstrlenA(text);
}

int main(int argc, char** argv)
{
    const char* name = argc > 1 ? argv[1] : "Fathom";
    printf("%u %s %lu\n", checksum((const unsigned char*)name, (unsigned)strlen(name)), describe(argc), greet(name));
    return 0;
}
