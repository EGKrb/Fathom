//
// This file is distributed under the MIT License. See LICENSE.md for details.
//

// Fathom, Windows: LLVM is built without assertions there, so the dump()
// methods of its classes (compiled only with assertions or LLVM_ENABLE_DUMP)
// are not in its libraries. rev.ng calls some of them: their definitions, as
// LLVM's AsmWriter.cpp has them.

#include "llvm/Config/llvm-config.h"
#include "llvm/IR/Value.h"
#include "llvm/Support/Debug.h"
#include "llvm/Support/raw_ostream.h"

#if defined(_WIN32) && !defined(LLVM_ENABLE_DUMP)
void llvm::Value::dump() const {
  print(dbgs(), /*IsForDebug=*/true);
  dbgs() << '\n';
}
#endif
