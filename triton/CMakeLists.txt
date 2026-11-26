cmake_minimum_required(VERSION 3.20)
project(fathom_triton C CXX)

# The Triton plugin of Fathom: symbolic execution and taint analysis (Triton, third_party/Triton) on the
# state of the debuggee or on a recorded Time Travel trace (.ftt). Dependencies, pinned:
#   - Triton (Apache-2.0): vendored in third_party/Triton (see third_party/Triton/FATHOM.md)
#   - Capstone 5.0.7 (BSD-3-Clause): built from its sources, static, x86 only
#   - Z3 4.16.0 (MIT): the official Windows build (libz3.dll ships next to the plugin)

set(X64DBG_SDK "$ENV{LOCALAPPDATA}/Microsoft/WinGet/Packages/x64dbg.x64dbg_Microsoft.Winget.Source_8wekyb3d8bbwe/pluginsdk"
    CACHE PATH "x64dbg pluginsdk directory")
if(NOT EXISTS "${X64DBG_SDK}/_plugins.h")
    message(FATAL_ERROR "x64dbg plugin SDK not found in '${X64DBG_SDK}', pass -DX64DBG_SDK=<path to pluginsdk>")
endif()

if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(ARCH x64)
    set(PLUGIN_EXT .dp64)
    set(Z3_ARCHIVE z3-4.16.0-x64-win)
    set(Z3_SHA256 de4d813e47202394a093547dbdb5699ee076529aa853463e007539775cd7e836)
else()
    set(ARCH x32)
    set(PLUGIN_EXT .dp32)
    set(Z3_ARCHIVE z3-4.16.0-x86-win)
    set(Z3_SHA256 74b5c8e22bceabc22c176601dffb222fcdf0df4e1fad8f004652a9a7bf7fc102)
endif()

include(FetchContent)
if(POLICY CMP0135)
    cmake_policy(SET CMP0135 NEW)
endif()

# ------------------------------------------------------------------------------------------------ Z3 ---
FetchContent_Declare(z3
    URL "https://github.com/Z3Prover/z3/releases/download/z3-4.16.0/${Z3_ARCHIVE}.zip"
    URL_HASH SHA256=${Z3_SHA256}
)
FetchContent_MakeAvailable(z3)
set(Z3_DIR_ROOT "${z3_SOURCE_DIR}")
set(Z3_INCLUDE_DIRS "${Z3_DIR_ROOT}/include" CACHE PATH "" FORCE)
set(Z3_LIBRARIES "${Z3_DIR_ROOT}/bin/libz3.lib" CACHE FILEPATH "" FORCE)
set(Z3_DLL "${Z3_DIR_ROOT}/bin/libz3.dll")

# ------------------------------------------------------------------------------------------ Capstone ---
set(CAPSTONE_ARCHITECTURE_DEFAULT OFF CACHE BOOL "" FORCE)
set(CAPSTONE_X86_SUPPORT ON CACHE BOOL "" FORCE)
set(CAPSTONE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(CAPSTONE_BUILD_CSTOOL OFF CACHE BOOL "" FORCE)
set(CAPSTONE_BUILD_CSTEST OFF CACHE BOOL "" FORCE)
set(CAPSTONE_INSTALL OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(capstone
    URL "https://github.com/capstone-engine/capstone/archive/d2e5739cbb43df93d589a3a726c8f878b95beccb.zip" # tag 5.0.7
    URL_HASH SHA256=43c4bfba511b55dcf717014f4db95be454887d848306c510cb2f52aa759a0883
)
FetchContent_MakeAvailable(capstone)
set(CAPSTONE_INCLUDE_DIRS "${capstone_SOURCE_DIR}/include" CACHE PATH "" FORCE)
set(CAPSTONE_LIBRARIES "$<BUILD_INTERFACE:capstone_static>" CACHE STRING "" FORCE) # not installed: kept out of Triton's export set

# ------------------------------------------------------------------------------------------- Triton ---
set(Z3_INTERFACE ON CACHE BOOL "" FORCE)
set(BITWUZLA_INTERFACE OFF CACHE BOOL "" FORCE)
set(LLVM_INTERFACE OFF CACHE BOOL "" FORCE)
set(BOOST_INTERFACE OFF CACHE BOOL "" FORCE)
set(PYTHON_BINDINGS OFF CACHE BOOL "" FORCE)
set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(ENABLE_TEST OFF CACHE BOOL "" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory(third_party/Triton EXCLUDE_FROM_ALL)

# ------------------------------------------------------------------------------------------- plugin ---
add_library(triton_plugin SHARED src/plugin.cpp src/session.cpp src/commands.cpp src/vmhunt.cpp)
target_compile_features(triton_plugin PRIVATE cxx_std_17)
target_include_directories(triton_plugin PRIVATE "${X64DBG_SDK}" "${CMAKE_CURRENT_SOURCE_DIR}/../src/dbg/ftt" "${Z3_INCLUDE_DIRS}" "${CAPSTONE_INCLUDE_DIRS}")
target_compile_definitions(triton_plugin PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)
if(ARCH STREQUAL "x64")
    set(LZ4_LIB "${CMAKE_CURRENT_SOURCE_DIR}/../src/dbg/lz4/lz4_x64.lib")
else()
    set(LZ4_LIB "${CMAKE_CURRENT_SOURCE_DIR}/../src/dbg/lz4/lz4_x86.lib")
endif()
target_link_libraries(triton_plugin PRIVATE triton "${X64DBG_SDK}/${ARCH}dbg.lib" "${X64DBG_SDK}/${ARCH}bridge.lib"
    "${LZ4_LIB}" delayimp)
# libz3.dll is loaded by the plugin from its own folder (plugin.cpp), before the first use of Z3
target_link_options(triton_plugin PRIVATE /DELAYLOAD:libz3.dll)
set_target_properties(triton_plugin PROPERTIES SUFFIX "${PLUGIN_EXT}" PREFIX "" OUTPUT_NAME "Triton")
if(MSVC)
    target_compile_options(triton_plugin PRIVATE /W4 /utf-8 /bigobj)
    target_compile_options(triton PRIVATE /bigobj /utf-8 /W0)
endif()
add_custom_command(TARGET triton_plugin POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${Z3_DLL}" "$<TARGET_FILE_DIR:triton_plugin>/libz3.dll")

# The test program of tests/test_triton.py (a crackme and an obfuscated function)
add_executable(triton_target tests/target.c)
if(MSVC)
    # optimized like a release build, without inlining the exported functions into main
    target_compile_options(triton_target PRIVATE /O1 /GS-)
endif()

# The virtual machine of the vm.detect / vm.simplify test (x86: MSVC inline assembly)
if(ARCH STREQUAL "x32")
    add_executable(vm_target tests/vm_target.c)
    target_compile_options(vm_target PRIVATE /O1 /GS-)
    add_dependencies(triton_target vm_target)
endif()
