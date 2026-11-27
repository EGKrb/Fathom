/*
 * Test program of NoVmp against VMProtect itself: functions whose result is known, virtualized by VMProtect
 * (its SDK's markers), then protected with VMProtect_Con (tests/protect_vmp_target.ps1). NoVmp must find their
 * virtual machines and lift them; the program checks its own results natively (exit code 0).
 *
 *   vmp_target            runs the checks
 *   vmp_target wait       the same, then waits for a key (to attach a debugger)
 */
#include <stdbool.h> /* VMProtectSDK.h uses bool */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "VMProtectSDK.h"

#define NOINLINE __declspec(noinline)
#define EXPORT __declspec(dllexport)

/* (x ^ y) + 2 * (x & y): x + y, obfuscated */
EXPORT NOINLINE uint32_t vmp_mba(uint32_t x, uint32_t y)
{
    uint32_t result;
    VMProtectBeginVirtualization("vmp_mba");
    result = (x ^ y) + 2 * (x & y);
    VMProtectEnd();
    return result;
}

/* a serial check: each byte transformed and compared */
EXPORT NOINLINE int vmp_check(const unsigned char *serial)
{
    static const unsigned char expected[8] = { 0x1b, 0x18, 0x19, 0x1e, 0x1f, 0x1c, 0x1d, 0x12 };
    int ok = 1;
    VMProtectBeginVirtualization("vmp_check");
    for (int i = 0; i < 8; i++) {
        if ((unsigned char)((serial[i] ^ 0x2a) + i) != (unsigned char)(expected[i] + i))
            ok = 0;
    }
    VMProtectEnd();
    return ok;
}

/* a loop with a multiplication: the sum of i * k for i < n */
EXPORT NOINLINE uint64_t vmp_sum(uint32_t n, uint32_t k)
{
    uint64_t sum = 0;
    VMProtectBeginVirtualization("vmp_sum");
    for (uint32_t i = 0; i < n; i++)
        sum += (uint64_t)i * k;
    VMProtectEnd();
    return sum;
}

int main(int argc, char **argv)
{
    int failures = 0;
    if (vmp_mba(0x12345678, 0x0fedcba9) != 0x12345678u + 0x0fedcba9u)
        failures++;
    if (!vmp_check((const unsigned char *)"12345678") || vmp_check((const unsigned char *)"12345679"))
        failures++;
    if (vmp_sum(100, 3) != 3 * 4950)
        failures++;
    printf("vmp_target: %d failure(s)\n", failures);
    if (argc > 1 && strcmp(argv[1], "wait") == 0)
        getchar();
    return failures;
}
