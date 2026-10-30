/* x86 test fixture for wineye's 32-bit unwinder (tests/test_debugger.py): FPO code
 * (FrameData program strings) called from a function that keeps an ebp frame.
 * Built without the CRT, at a fixed base:
 *   cl /nologo /Zi /O2 /Oy /GS- /Fd:fixture32_c.pdb fixture32.c /link /DEBUG /NODEFAULTLIB
 *      /ENTRY:entry /SUBSYSTEM:CONSOLE /SAFESEH:NO /DYNAMICBASE:NO /BASE:0x400000
 *      /INCREMENTAL:NO /PDB:fixture32.pdb /PDBALTPATH:fixture32.pdb /OUT:fixture32.exe
 */
static volatile int spin = 1;

__declspec(noinline) int __stdcall inner32(int depth, int *out)
{
    int a = depth * 3, b = depth + 5, c = depth ^ 0x55;
    while (spin) {
        a += b;
        b ^= c;
        c += a;
    }
    *out = a;
    return b + c;
}

#pragma optimize("y", off)
__declspec(noinline) int __cdecl outer32(int seed)
{
    int value = 0;
    int r = inner32(seed, &value);
    return r + value;
}
#pragma optimize("", on)

int __stdcall entry(void)
{
    return outer32(7);
}
