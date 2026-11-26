/*
 * This file is distributed under the MIT License. See LICENSE.md for details.
 */

#include <stdint.h>

#include "revng/Runtime/PlainMetaAddress.h"

#include "support.h"

PlainMetaAddress last_pc;
PlainMetaAddress current_pc;

// Fathom, Windows: MinGW's setjmp is a macro around _setjmp, and rev.ng looks
// for a function named setjmp in this module: declare one
#if defined(_WIN32) && defined(setjmp)
#undef setjmp
int setjmp(jmp_buf Buffer);
#endif
#define REVNG_SETJMP_FUNCTION setjmp

// The only purpose of this function is keeping alive the references to some
// symbols that are needed by revng
intptr_t _ugly_name_ignore(void);
intptr_t _ugly_name_ignore(void) {
  return (intptr_t) &saved_registers + (intptr_t) &REVNG_SETJMP_FUNCTION
         + (intptr_t) &jmp_buffer + (intptr_t) &is_executable
         + (intptr_t) &unknown_pc + (intptr_t) &revng_abort;
}

void set_PlainMetaAddress(PlainMetaAddress *This,
                          uint32_t Epoch,
                          uint16_t AddressSpace,
                          uint16_t Type,
                          uint64_t Address) {
  This->Epoch = Epoch;
  This->AddressSpace = AddressSpace;
  This->Type = Type;
  This->Address = Address;
}
