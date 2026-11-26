// Fathom: what depends on the architecture NoVmp is built for. NoVmp.exe (x64) devirtualizes VMProtect's x64
// virtual machine; NoVmp32.exe, the same sources built for x86 (VTIL selects its x86 architecture when it is
// built for x86), devirtualizes the x86 one. The handlers are matched on the instructions of the target
// architecture, decoded into VTIL's instruction structure (the same for both).
#pragma once
#include <vector>
#include <stdint.h>
#include <linuxpe>
#include <vtil/amd64>
#include <vtil/x86>

namespace vmp
{
#if defined( _M_IX86 ) || defined( __i386__ )
	constexpr bool native_x86 = true;
	// Bytes of a native register, of a VM stack word for the flags and native-size operands
	constexpr int native_size = 4;
	constexpr x86_reg REG_NSP = X86_REG_ESP;
	constexpr x86_reg REG_NIP = X86_REG_EIP;
	constexpr x86_reg REG_NAX = X86_REG_EAX;
	constexpr x86_reg REG_NBX = X86_REG_EBX;
	constexpr x86_reg REG_NCX = X86_REG_ECX;
	constexpr x86_reg REG_NDX = X86_REG_EDX;
	constexpr x86_insn INS_PUSHF = X86_INS_PUSHFD;
	constexpr x86_insn INS_POPF = X86_INS_POPFD;
	using image_t = win::image_x86_t;
	using nt_headers_t = win::nt_headers_x86_t;
	// The TEB: fs on x86
	constexpr x86_prefix TEB_PREFIX = X86_PREFIX_FS;
	constexpr const char* TEB_SELF = "mov eax, fs:0x18";
	// The base VMProtect's encrypted VIP is relative to (-vipbase overrides it): not verified on x86
	inline uint64_t default_vip_base = 0;

	// x86 code, in the structure NoVmp works on (identical in VTIL for both architectures)
	inline std::vector<vtil::amd64::instruction> disasm( const void* bytes, uint64_t address, size_t size = 0, size_t count = 1 )
	{
		std::vector<vtil::amd64::instruction> out;
		for ( auto& in : vtil::x86::disasm( bytes, address, size, count ) )
		{
			vtil::amd64::instruction ins;
			ins.id = in.id;
			ins.address = in.address;
			ins.bytes = in.bytes;
			ins.mnemonic = in.mnemonic;
			ins.operand_string = in.operand_string;
			ins.regs_read = in.regs_read;
			ins.regs_write = in.regs_write;
			ins.groups = in.groups;
			std::copy( std::begin( in.prefix ), std::end( in.prefix ), std::begin( ins.prefix ) );
			ins.opcode = in.opcode;
			ins.rex = in.rex;
			ins.addr_size = in.addr_size;
			ins.modrm = in.modrm;
			ins.sib = in.sib;
			ins.disp = in.disp;
			ins.sib_index = in.sib_index;
			ins.sib_scale = in.sib_scale;
			ins.sib_base = in.sib_base;
			ins.xop_cc = in.xop_cc;
			ins.sse_cc = in.sse_cc;
			ins.avx_cc = in.avx_cc;
			ins.avx_sae = in.avx_sae;
			ins.avx_rm = in.avx_rm;
			ins.eflags = in.eflags;
			ins.operands = in.operands;
			ins.encoding = in.encoding;
			out.push_back( std::move( ins ) );
		}
		return out;
	}
#else
	constexpr bool native_x86 = false;
	constexpr int native_size = 8;
	constexpr x86_reg REG_NSP = X86_REG_RSP;
	constexpr x86_reg REG_NIP = X86_REG_RIP;
	constexpr x86_reg REG_NAX = X86_REG_RAX;
	constexpr x86_reg REG_NBX = X86_REG_RBX;
	constexpr x86_reg REG_NCX = X86_REG_RCX;
	constexpr x86_reg REG_NDX = X86_REG_RDX;
	constexpr x86_insn INS_PUSHF = X86_INS_PUSHFQ;
	constexpr x86_insn INS_POPF = X86_INS_POPFQ;
	using image_t = win::image_x64_t;
	using nt_headers_t = win::nt_headers_x64_t;
	constexpr x86_prefix TEB_PREFIX = X86_PREFIX_GS;
	constexpr const char* TEB_SELF = "mov rax, gs:0x30";
	inline uint64_t default_vip_base = 0x100000000;

	inline std::vector<vtil::amd64::instruction> disasm( const void* bytes, uint64_t address, size_t size = 0, size_t count = 1 )
	{
		return vtil::amd64::disasm( bytes, address, size, count );
	}
#endif

	constexpr int native_bits = native_size * 8;

	// The register of base reg with the given size in bytes (AL / AX / EAX / RAX...)
	inline x86_reg resize( x86_reg reg, int size )
	{
		return vtil::amd64::registers.remap( reg, 0, size );
	}

	// The self-reference of a handler: the address of the instruction itself loaded in a register, by
	// LEA r64, [RIP - 7] on x64, by MOV r32, imm32 (its own address, relocated) on x86
	inline bool is_self_ref( const vtil::amd64::instruction& ins, uint64_t image_base )
	{
		if constexpr ( native_x86 )
		{
			return ins.is( X86_INS_MOV, { X86_OP_REG, X86_OP_IMM } ) &&
				( uint64_t ) ( uint32_t ) ins.operands[ 1 ].imm == ( uint64_t ) ( uint32_t ) ( image_base + ins.address );
		}
		else
		{
			return ins.is( X86_INS_LEA, { X86_OP_REG, X86_OP_MEM } ) &&
				ins.operands[ 1 ].mem.disp == -7 &&
				ins.operands[ 1 ].mem.scale == 1 &&
				ins.operands[ 1 ].mem.base == X86_REG_RIP &&
				ins.operands[ 1 ].mem.index == X86_REG_INVALID;
		}
	}

	// The load of the relocation delta at the end of the context save: MOVABS r64, imm64 / MOV r32, imm32
	inline bool is_reloc_load( const vtil::amd64::instruction& ins )
	{
		if constexpr ( native_x86 )
			return ins.is( X86_INS_MOV, { X86_OP_REG, X86_OP_IMM } );
		else
			return ins.is( X86_INS_MOVABS, { X86_OP_REG, X86_OP_IMM } );
	}
};
