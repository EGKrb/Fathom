// vtildump: prints VTIL routines (.vtil files, as NoVmp saves them) as text, with VTIL's debug dump.
// Used by the novmp command of Fathom's ExtraCmds plugin.
//
//   vtildump file.vtil [...]
#include <vtil/vtil>

#include <cstdio>
#include <exception>

int main( int argc, const char** argv )
{
	if ( argc < 2 )
	{
		std::fprintf( stderr, "vtildump file.vtil [...]\n" );
		return 2;
	}
	int failures = 0;
	for ( int i = 1; i < argc; i++ )
	{
		try
		{
			vtil::routine* rtn = vtil::load_routine( argv[ i ] );
			std::printf( "%s: %zu blocks\n", argv[ i ], rtn->explored_blocks.size() );
			vtil::debug::dump( rtn );
			// not deleted: the process ends (VTIL's ownership of the blocks asserts when a loaded routine is deleted)
		}
		catch ( const std::exception& ex )
		{
			std::fprintf( stderr, "%s: %s\n", argv[ i ], ex.what() );
			failures++;
		}
	}
	return failures;
}
