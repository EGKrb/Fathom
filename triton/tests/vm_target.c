// Test program of vm.detect / vm.simplify (tests/test_triton.py, x86): a small virtual machine in the
// style of the protectors. vm_compute saves the whole context (pushfd, pushad), then a dispatcher reads
// bytecode and jumps through a handler table; the handlers work on the saved registers on the stack; the
// exit restores the context (popad, popfd). The bytecode computes eax = (eax ^ 0x5A) + ebx * 3.
//
//   vm_target.exe        prints run_vm(40, 2) = (40 ^ 0x5A) + 6 = 120
#include <stdio.h>

#ifndef _M_IX86
#error "x86 only (MSVC inline assembly)"
#endif

void vm_dispatch(void);
void vm_load(void);
void vm_xor(void);
void vm_add3(void);
void vm_store(void);
void vm_exit(void);

// pushad order on the stack: [esp] edi, esi, ebp, esp, ebx, edx, ecx, eax -> slot 7 = eax, slot 4 = ebx
static const unsigned char bytecode[] =
{
    0, 7,                       // load  acc = eax
    1, 0x5A, 0, 0, 0,           // xor   acc ^= 0x5A
    2, 4,                       // add3  acc += 3 * ebx
    3, 7,                       // store eax = acc
    4,                          // exit
};

void* vm_handlers[] = { vm_load, vm_xor, vm_add3, vm_store, vm_exit };

// esi: bytecode pointer, edx: accumulator, the saved context at [esp]
__declspec(naked) void vm_dispatch(void)
{
    __asm
    {
        movzx eax, byte ptr [esi]
        inc esi
        jmp dword ptr [vm_handlers + eax * 4]
    }
}

__declspec(naked) void vm_load(void)
{
    __asm
    {
        movzx eax, byte ptr [esi]
        inc esi
        mov edx, [esp + eax * 4]
        jmp vm_dispatch
    }
}

__declspec(naked) void vm_xor(void)
{
    __asm
    {
        xor edx, [esi]
        add esi, 4
        jmp vm_dispatch
    }
}

__declspec(naked) void vm_add3(void)
{
    __asm
    {
        movzx eax, byte ptr [esi]
        inc esi
        mov ecx, [esp + eax * 4]
        lea ecx, [ecx + ecx * 2]
        add edx, ecx
        jmp vm_dispatch
    }
}

__declspec(naked) void vm_store(void)
{
    __asm
    {
        movzx eax, byte ptr [esi]
        inc esi
        mov [esp + eax * 4], edx
        jmp vm_dispatch
    }
}

__declspec(naked) void vm_exit(void)
{
    __asm
    {
        popad
        popfd
        ret
    }
}

// the entry of the virtual machine: inputs in eax and ebx, the result in eax
__declspec(naked) void vm_compute(void)
{
    __asm
    {
        pushfd
        pushad
        lea esi, bytecode
        jmp vm_dispatch
    }
}

__declspec(dllexport) __declspec(naked) unsigned __cdecl run_vm(unsigned a, unsigned b)
{
    __asm
    {
        push ebx
        mov eax, [esp + 8]
        mov ebx, [esp + 12]
        call vm_compute
        pop ebx
        ret
    }
}

int main(void)
{
    unsigned result = run_vm(40, 2);
    printf("run_vm(40, 2) = %u\n", result);
    return result == ((40 ^ 0x5A) + 2 * 3) ? 0 : 1;
}
