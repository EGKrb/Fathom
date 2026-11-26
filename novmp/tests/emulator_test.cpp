// Test of NoVmp's emulator, the part that runs the decryption blocks of VMProtect natively: built for x64
// (NoVmp's shellcode) and for x86 (Fathom's invoke, NoVmp32). Blocks of register arithmetic, the shape of
// VMProtect's rolling-key decryption, are run and their result compared with the same computation in C++.
//
//   emulator_test[32].exe     exit code 0 if every block gives the expected registers
#include "../third_party/NoVmp/NoVmp/emulator/emulator.hpp"
#include "../third_party/NoVmp/NoVmp/emulator/rwx_allocator.hpp"

#include <cstdio>
#include <cstdlib>
#include <vector>

static uint32_t rol32( uint32_t v, int n ) { return ( v << n ) | ( v >> ( 32 - n ) ); }
static uint32_t ror32( uint32_t v, int n ) { return ( v >> n ) | ( v << ( 32 - n ) ); }
static uint32_t bswap32( uint32_t v ) { return ( v >> 24 ) | ( ( v >> 8 ) & 0xFF00 ) | ( ( v << 8 ) & 0xFF0000 ) | ( v << 24 ); }

static int failures = 0;

static void check( const char* what, uint64_t got, uint64_t expected )
{
	if ( got != expected )
	{
		std::printf( "FAIL %s: %llX, expected %llX\n", what, ( unsigned long long ) got, ( unsigned long long ) expected );
		failures++;
	}
}

static emulator run( const std::vector<uint8_t>& code, uint32_t eax, uint32_t ebx, uint32_t ecx, uint32_t edx, uint32_t esi )
{
	std::vector<uint8_t, mem::rwx_allocator<uint8_t>> exec( code.begin(), code.end() );
	exec.push_back( 0xC3 ); // ret
	emulator emu = {};
	emu.set( X86_REG_EAX, eax ).set( X86_REG_EBX, ebx ).set( X86_REG_ECX, ecx ).set( X86_REG_EDX, edx ).set( X86_REG_ESI, esi );
	emu.invoke( exec.data() );
	return emu;
}

int main()
{
	// Encodings valid in 32 and 64-bit mode (32-bit operands, no REX)
	// xor eax, ebx ; rol eax, 5 ; add ecx, eax ; bswap edx ; not esi ; ror ebx, 3 ; sub ecx, 0x1234567
	const std::vector<uint8_t> block1 = {
		0x31, 0xD8,                   // xor eax, ebx
		0xC1, 0xC0, 0x05,             // rol eax, 5
		0x01, 0xC1,                   // add ecx, eax
		0x0F, 0xCA,                   // bswap edx
		0xF7, 0xD6,                   // not esi
		0xC1, 0xCB, 0x03,             // ror ebx, 3
		0x81, 0xE9, 0x67, 0x45, 0x23, 0x01, // sub ecx, 0x1234567
	};
	for ( uint32_t seed = 1; seed < 2000; seed++ )
	{
		uint32_t a = seed * 2654435761u, b = seed * 40503u ^ 0xDEADBEEF, c = ~seed * 7919u, d = seed << 7 | 0x55, s = seed * 31u;
		emulator emu = run( block1, a, b, c, d, s );
		uint32_t ea = rol32( a ^ b, 5 );
		uint32_t ec = c + ea - 0x1234567;
		check( "eax", emu.get( X86_REG_EAX ), ea );
		check( "ebx", emu.get( X86_REG_EBX ), ror32( b, 3 ) );
		check( "ecx", emu.get( X86_REG_ECX ), ec );
		check( "edx", emu.get( X86_REG_EDX ), bswap32( d ) );
		check( "esi", emu.get( X86_REG_ESI ), ~s );
		if ( failures ) break;
	}
	// 8 and 16-bit parts, as VMProtect decrypts one-byte and two-byte operands: add al, bl ; xor ax, 0x1F2E ; neg cl ; dec dx
	const std::vector<uint8_t> block2 = {
		0x00, 0xD8,                   // add al, bl
		0x66, 0x35, 0x2E, 0x1F,       // xor ax, 0x1F2E
		0xF6, 0xD9,                   // neg cl
		0x66, 0x4A,                   // dec dx (32-bit mode encoding; in 64-bit mode 0x4A is a REX prefix)
	};
	if ( sizeof( void* ) == 4 )
	{
		emulator emu = run( block2, 0x11223344, 0x000000F0, 0x00000005, 0x00010000, 0 );
		uint32_t a = 0x11223344;
		a = ( a & ~0xFFu ) | ( ( a + 0xF0 ) & 0xFF );
		a = ( a & ~0xFFFFu ) | ( ( a ^ 0x1F2E ) & 0xFFFF );
		check( "al/ax", emu.get( X86_REG_EAX ), a );
		check( "cl", emu.get( X86_REG_ECX ), ( uint8_t ) -5 );
		check( "dx", emu.get( X86_REG_EDX ), 0x0001FFFF );
	}
	// the flags of the block come back: stc ; cmc ; stc -> CF set
	const std::vector<uint8_t> block3 = { 0xF9, 0xF5, 0xF9 };
	{
		emulator emu = run( block3, 0, 0, 0, 0, 0 );
		check( "CF", emu.v_rflags & 1, 1 );
	}
	std::printf( "%s: %s\n", sizeof( void* ) == 4 ? "x86 (NoVmp32)" : "x64 (NoVmp)", failures ? "FAILED" : "OK, 2000 blocks and the flags" );
	return failures ? 1 : 0;
}
