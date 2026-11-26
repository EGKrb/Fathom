// Test program of the Triton plugin (tests/test_triton.py): a "crackme" whose serial is checked in two
// ways, and an obfuscated function. The functions are exported so that the tests find them by name.
//
//   triton_target.exe SERIAL     exit code: bit 0 set if check_mix rejects SERIAL, bit 1 if
//                                check_branches does (they want different serials: the tests solve
//                                one at a time)
#include <stdio.h>
#include <string.h>

#define SERIAL_LENGTH 8

// Without branches: every character is mixed into one difference, compared once at the end
__declspec(dllexport) __declspec(noinline) int check_mix(const unsigned char* serial)
{
    static const unsigned char expected[SERIAL_LENGTH] = { 0x3C, 0x2E, 0x6D, 0x15, 0x58, 0x7B, 0x40, 0x2A };
    unsigned difference = 0;
    for(int i = 0; i < SERIAL_LENGTH; i++)
        difference |= (unsigned)(((serial[i] ^ 0x5A) + i * 7) & 0xFF) ^ expected[i];
    return difference == 0;
}

// With branches: one comparison per character, the first wrong one returns
__declspec(dllexport) __declspec(noinline) int check_branches(const unsigned char* serial)
{
    for(int i = 0; i < SERIAL_LENGTH; i++)
    {
        if((unsigned char)(serial[i] * 3 + 1) != (unsigned char)(0x41 + i * 11))
            return 0;
    }
    return 1;
}

// x + y, written as a mixed boolean-arithmetic expression (what obfuscators do)
__declspec(dllexport) __declspec(noinline) unsigned mba(unsigned x, unsigned y)
{
    volatile unsigned a = x, b = y;
    return (a ^ b) + 2 * (a & b);
}

int main(int argc, char** argv)
{
    unsigned char serial[SERIAL_LENGTH + 1] = "AAAAAAAA";
    if(argc > 1)
        strncpy((char*)serial, argv[1], SERIAL_LENGTH);
    int code = 0;
    if(!check_mix(serial))
        code |= 1;
    if(!check_branches(serial))
        code |= 2;
    printf("%s (mba %u)\n", code ? "wrong serial" : "right serial", mba(40, 2));
    return code;
}
