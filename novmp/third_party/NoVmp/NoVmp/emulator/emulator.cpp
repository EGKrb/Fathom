// Copyright (c) 2020 Can Boluk and contributors of the VTIL Project   
// All rights reserved.   
//    
// Redistribution and use in source and binary forms, with or without   
// modification, are permitted provided that the following conditions are met: 
//    
// 1. Redistributions of source code must retain the above copyright notice,   
//    this list of conditions and the following disclaimer.   
// 2. Redistributions in binary form must reproduce the above copyright   
//    notice, this list of conditions and the following disclaimer in the   
//    documentation and/or other materials provided with the distribution.   
// 3. Neither the name of VTIL Project nor the names of its contributors
//    may be used to endorse or promote products derived from this software 
//    without specific prior written permission.   
//    
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" 
// AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE   
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE  
// ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE   
// LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR   
// CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF   
// SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS   
// INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN   
// CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)   
// ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE  
// POSSIBILITY OF SUCH DAMAGE.        
//
#include "emulator.hpp"
#include <vtil/io>
#include "rwx_allocator.hpp"

#ifndef _WIN32
#define __stdcall __attribute__ ( ( ms_abi ) )
#endif

/*
	mov     rax, rsp
	mov     rsp, rcx
	add     rsp, 20h
	mov     [rsp+188h], rax

	lea rax, qword ptr [rsp+178h]

	push    qword ptr [rax]
	pushfq
	pop     qword ptr [rax]
	popfq

	xchg    rax, [rsp+100h]
	xchg    rbx, [rsp+108h]
	xchg    rcx, [rsp+110h]
	xchg    rdx, [rsp+118h]
	xchg    rsi, [rsp+120h]
	xchg    rdi, [rsp+128h]
	xchg    rbp, [rsp+130h]
	xchg    r8, [rsp+138h]
	xchg    r9, [rsp+140h]
	xchg    r10, [rsp+148h]
	xchg    r11, [rsp+150h]
	xchg    r12, [rsp+158h]
	xchg    r13, [rsp+160h]
	xchg    r14, [rsp+168h]
	xchg    r15, [rsp+170h]

	call    qword ptr [rsp+180h]

	xchg    rax, [rsp+100h]
	xchg    rbx, [rsp+108h]
	xchg    rcx, [rsp+110h]
	xchg    rdx, [rsp+118h]
	xchg    rsi, [rsp+120h]
	xchg    rdi, [rsp+128h]
	xchg    rbp, [rsp+130h]
	xchg    r8, [rsp+138h]
	xchg    r9, [rsp+140h]
	xchg    r10, [rsp+148h]
	xchg    r11, [rsp+150h]
	xchg    r12, [rsp+158h]
	xchg    r13, [rsp+160h]
	xchg    r14, [rsp+168h]
	xchg    r15, [rsp+170h]

	lea rax, qword ptr [rsp+178h]

	push    qword ptr [rax]
	pushfq
	pop     qword ptr [rax]
	popfq


	mov     rsp, [rsp+188h]
	ret
*/
#if !defined( _M_IX86 )
static const std::vector<uint8_t, mem::rwx_allocator<uint8_t>> emulator_shellcode = {
	0x48, 0x89, 0xE0, 0x48, 0x89, 0xCC, 0x48, 0x83, 0xC4, 0x20, 0x48, 0x89, 0x84, 0x24,
	0x88, 0x01, 0x00, 0x00, 0x48, 0x8D, 0x84, 0x24, 0x78, 0x01, 0x00, 0x00, 0xFF, 0x30,
	0x9C, 0x8F, 0x00, 0x9D, 0x48, 0x87, 0x84, 0x24, 0x00, 0x01, 0x00, 0x00, 0x48, 0x87,
	0x9C, 0x24, 0x08, 0x01, 0x00, 0x00, 0x48, 0x87, 0x8C, 0x24, 0x10, 0x01, 0x00, 0x00,
	0x48, 0x87, 0x94, 0x24, 0x18, 0x01, 0x00, 0x00, 0x48, 0x87, 0xB4, 0x24, 0x20, 0x01,
	0x00, 0x00, 0x48, 0x87, 0xBC, 0x24, 0x28, 0x01, 0x00, 0x00, 0x48, 0x87, 0xAC, 0x24,
	0x30, 0x01, 0x00, 0x00, 0x4C, 0x87, 0x84, 0x24, 0x38, 0x01, 0x00, 0x00, 0x4C, 0x87,
	0x8C, 0x24, 0x40, 0x01, 0x00, 0x00, 0x4C, 0x87, 0x94, 0x24, 0x48, 0x01, 0x00, 0x00,
	0x4C, 0x87, 0x9C, 0x24, 0x50, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xA4, 0x24, 0x58, 0x01,
	0x00, 0x00, 0x4C, 0x87, 0xAC, 0x24, 0x60, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xB4, 0x24,
	0x68, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xBC, 0x24, 0x70, 0x01, 0x00, 0x00, 0xFF, 0x94,
	0x24, 0x80, 0x01, 0x00, 0x00, 0x48, 0x87, 0x84, 0x24, 0x00, 0x01, 0x00, 0x00, 0x48,
	0x87, 0x9C, 0x24, 0x08, 0x01, 0x00, 0x00, 0x48, 0x87, 0x8C, 0x24, 0x10, 0x01, 0x00,
	0x00, 0x48, 0x87, 0x94, 0x24, 0x18, 0x01, 0x00, 0x00, 0x48, 0x87, 0xB4, 0x24, 0x20,
	0x01, 0x00, 0x00, 0x48, 0x87, 0xBC, 0x24, 0x28, 0x01, 0x00, 0x00, 0x48, 0x87, 0xAC,
	0x24, 0x30, 0x01, 0x00, 0x00, 0x4C, 0x87, 0x84, 0x24, 0x38, 0x01, 0x00, 0x00, 0x4C,
	0x87, 0x8C, 0x24, 0x40, 0x01, 0x00, 0x00, 0x4C, 0x87, 0x94, 0x24, 0x48, 0x01, 0x00,
	0x00, 0x4C, 0x87, 0x9C, 0x24, 0x50, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xA4, 0x24, 0x58,
	0x01, 0x00, 0x00, 0x4C, 0x87, 0xAC, 0x24, 0x60, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xB4,
	0x24, 0x68, 0x01, 0x00, 0x00, 0x4C, 0x87, 0xBC, 0x24, 0x70, 0x01, 0x00, 0x00, 0x48,
	0x8D, 0x84, 0x24, 0x78, 0x01, 0x00, 0x00, 0xFF, 0x30, 0x9C, 0x8F, 0x00, 0x9D, 0x48,
	0x8B, 0xA4, 0x24, 0x88, 0x01, 0x00, 0x00, 0xC3
};

// Invokes routine at the pointer given with the current context and updates the context.
// - Template argument is a small trick to make it work with ICC, declaring a constexpr within the scope does not work.
//
void emulator::invoke( const void* routine_pointer )
{
    // Set the runtime RIP.
    //
    __rip = routine_pointer;

	// Invoke shellcode.
	//
	( ( void( __stdcall* )( emulator* ) )emulator_shellcode.data() )( this );
}
#else
// Fathom, x86 (NoVmp32): the block runs natively in this 32-bit process, on the 32-bit registers of the
// context and a stack of its own (the user stack of the structure). The context is addressed through ESI
// only: EBP, the frame pointer of this function, belongs to the block while it runs.
void emulator::invoke( const void* routine_pointer )
{
	__rip = routine_pointer;
	// volatile: the assembly writes it through ESI, which the optimizer does not see
	volatile uint32_t ctx[ 11 ] =
	{
		( uint32_t ) v_rax, ( uint32_t ) v_rbx, ( uint32_t ) v_rcx, ( uint32_t ) v_rdx,
		( uint32_t ) v_rsi, ( uint32_t ) v_rdi, ( uint32_t ) v_rbp, ( uint32_t ) v_rflags,
		( uint32_t ) routine_pointer,
		( uint32_t ) ( ( uint8_t* ) v_stack + sizeof( v_stack ) - 0x10 ),
		0
	};
	volatile uint32_t* c = ctx;
	__asm
	{
		pushad
		pushfd
		mov esi, c
		mov [ esi + 40 ], esp           // the host's stack
		mov eax, [ esi + 28 ]
		mov esp, [ esi + 36 ]           // the block's stack
		push esi                        // the context
		push dword ptr [ esi + 32 ]     // the block
		push eax
		popfd                           // the block's flags
		mov eax, [ esi + 0 ]
		mov ebx, [ esi + 4 ]
		mov ecx, [ esi + 8 ]
		mov edx, [ esi + 12 ]
		mov edi, [ esi + 20 ]
		mov ebp, [ esi + 24 ]
		mov esi, [ esi + 16 ]
		call dword ptr [ esp ]
		pushfd
		xchg esi, [ esp + 8 ]           // esi: the context; [esp + 8]: the block's esi
		mov [ esi + 0 ], eax
		mov [ esi + 4 ], ebx
		mov [ esi + 8 ], ecx
		mov [ esi + 12 ], edx
		mov [ esi + 20 ], edi
		mov [ esi + 24 ], ebp
		pop dword ptr [ esi + 28 ]      // its flags
		add esp, 4                      // the block's address
		pop dword ptr [ esi + 16 ]      // its esi
		mov esp, [ esi + 40 ]
		popfd
		popad
	}
	v_rax = ctx[ 0 ]; v_rbx = ctx[ 1 ]; v_rcx = ctx[ 2 ]; v_rdx = ctx[ 3 ];
	v_rsi = ctx[ 4 ]; v_rdi = ctx[ 5 ]; v_rbp = ctx[ 6 ]; v_rflags = ctx[ 7 ];
}
#endif

// Resolves the offset<0> where the value is saved at for the given register
// and the number of bytes<1> it takes.
//
std::pair<int32_t, uint8_t> emulator::resolve( x86_reg reg ) const
{
    auto [base_reg, offset, size] = vtil::amd64::registers.resolve_mapping( reg );

    const void* base;
    switch ( base_reg )
    {
        case X86_REG_RAX:	base = &v_rax;					break;
        case X86_REG_RBP:	base = &v_rbp;					break;
        case X86_REG_RBX:	base = &v_rbx;					break;
        case X86_REG_RCX:	base = &v_rcx;					break;
        case X86_REG_RDI:	base = &v_rdi;					break;
        case X86_REG_RDX:	base = &v_rdx;					break;
        case X86_REG_RSI:	base = &v_rsi;					break;
        case X86_REG_R8: 	base = &v_r8;					break;
        case X86_REG_R9: 	base = &v_r9;					break;
        case X86_REG_R10:	base = &v_r10;					break;
        case X86_REG_R11:	base = &v_r11;					break;
        case X86_REG_R12:	base = &v_r12;					break;
        case X86_REG_R13:	base = &v_r13;					break;
        case X86_REG_R14:	base = &v_r14;					break;
        case X86_REG_R15:	base = &v_r15;					break;
        default:            unreachable();
    }

    return { ( ( uint8_t* ) base - ( uint8_t* ) this ) + offset, size };
}

// Sets the value of a register.
//
emulator& emulator::set( x86_reg reg, uint64_t value )
{
    auto [off, sz] = resolve( reg );
    memcpy( ( uint8_t* ) this + off, &value, sz );
    return *this;
}

// Gets the value of a register.
//
uint64_t emulator::get( x86_reg reg ) const
{
    uint64_t value = 0;
    auto [off, sz] = resolve( reg );
    memcpy( &value, ( uint8_t* ) this + off, sz );
    return value;
}
