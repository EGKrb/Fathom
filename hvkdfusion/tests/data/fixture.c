/* Test fixture for wineye's PDB reader (tests/test_debugger.py).
 * Built without the CRT so that the private PDB stays small:
 *   cl /nologo /Zi /Od /GS- /Fd:fixture_c.pdb fixture.c /link /DEBUG /NODEFAULTLIB
 *      /ENTRY:entry /SUBSYSTEM:CONSOLE /PDB:fixture.pdb /OUT:fixture.exe
 */
typedef struct _POINT3 { int x; int y; long long z; } POINT3;

__declspec(noinline) int inner(int depth, POINT3 *pt)
{
    int local_counter = depth * 3;
    char buffer[32];
    buffer[0] = 'w';
    buffer[1] = 0;
    local_counter += pt->x;
    return local_counter + buffer[0];
}

__declspec(noinline) long long outer(int seed)
{
    POINT3 p = { seed, seed + 1, 0x1122334455667788LL };
    int result = inner(seed, &p);
    return result + p.z;
}

int entry(void)
{
    return (int)outer(7);
}
