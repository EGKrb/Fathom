#
# This file is distributed under the MIT License. See LICENSE.md for details.
#

# ~~~
# Fathom, Windows: revng-opt, LLVM's opt (llvm/: its sources, from rev.ng's
# LLVM 16, Apache-2.0 WITH LLVM-exception) with all of rev.ng linked in, where
# `revng opt` loads rev.ng's libraries into opt as plugins (-load) elsewhere.
# ~~~
revng_add_executable(revng-opt llvm/AnalysisWrappers.cpp
                     llvm/BreakpointPrinter.cpp llvm/NewPMDriver.cpp llvm/opt.cpp)

llvm_map_components_to_libnames(
  OPT_LLVM_LIBRARIES
  AllTargetsAsmParsers
  AllTargetsCodeGens
  AllTargetsDescs
  AllTargetsInfos
  AggressiveInstCombine
  Analysis
  AsmParser
  BitWriter
  CFGuard
  CodeGen
  Core
  Coroutines
  Extensions
  IPO
  IRReader
  IRPrinter
  InstCombine
  Instrumentation
  MC
  ObjCARCOpts
  Remarks
  ScalarOpts
  Support
  Target
  TargetParser
  TransformUtils
  Vectorize
  Passes)
target_link_libraries(revng-opt ${OPT_LLVM_LIBRARIES})
# opt.cpp and its warnings are LLVM's
target_compile_options(revng-opt PRIVATE -Wno-unused-parameter)
