cmake_minimum_required(VERSION 3.20)
project(fathom_novmp C CXX)

# NoVmp (GPL-3.0, third_party/NoVmp): the static devirtualizer of VMProtect 3.0 - 3.5, built on VTIL
# (BSD-3-Clause, third_party/VTIL-Core). VTIL fetches Capstone and Keystone at the commits it pins; NoVmp's
# linux-pe (MIT) is fetched here at the commit NoVmp pins.
#
# Built for x64: NoVmp.exe, VMProtect's x64 virtual machine (NoVmp's own target). Built for x86 (Fathom's
# port): NoVmp32.exe, the x86 virtual machine; VTIL then selects its x86 architecture (registers, stack,
# calling convention) and NoVmp the x86 forms of the handlers (vmprotect/native.hpp).

include(FetchContent)
# the submodule commit of NoVmp: no branch holds it any more (git cannot check it out), GitHub's archive does
if(POLICY CMP0135)
    cmake_policy(SET CMP0135 NEW)
endif()
FetchContent_Declare(linuxpe
    URL https://github.com/can1357/linux-pe/archive/30aae563eda7c6808ed4a376abee6bfd1a46f00a.zip
    URL_HASH SHA256=f59faea2b5095c380737846dba82144d4e1ab8a9769dd50ff38daff00703e16a
)
FetchContent_GetProperties(linuxpe)
if(NOT linuxpe_POPULATED)
    FetchContent_Populate(linuxpe) # header-only: its own CMake file is not needed
endif()

set(VTIL_BUILD_TESTS OFF CACHE BOOL "" FORCE)
add_subdirectory(third_party/VTIL-Core)

file(GLOB_RECURSE NOVMP_SOURCES CONFIGURE_DEPENDS third_party/NoVmp/NoVmp/*.cpp)
add_executable(NoVmp ${NOVMP_SOURCES} third_party/NoVmp/NoVmp/NoVmp.rc)
target_include_directories(NoVmp PRIVATE "${linuxpe_SOURCE_DIR}/includes")
target_link_libraries(NoVmp PRIVATE VTIL)
if(MSVC)
    target_compile_options(NoVmp PRIVATE /bigobj /W0 /utf-8)
endif()
if(CMAKE_SIZEOF_VOID_P EQUAL 4)
    set_target_properties(NoVmp PROPERTIES OUTPUT_NAME NoVmp32)
    # the emulator's x86 invoke uses inline assembly, and changes the frame pointer on purpose
    target_compile_options(NoVmp PRIVATE /wd4731)
endif()

# vtildump: the lifted routines as text (the novmp command prints them)
add_executable(vtildump tools/vtildump.cpp)
target_link_libraries(vtildump PRIVATE VTIL)
if(MSVC)
    target_compile_options(vtildump PRIVATE /bigobj /W0 /utf-8)
endif()
if(CMAKE_SIZEOF_VOID_P EQUAL 4)
    set_target_properties(vtildump PROPERTIES OUTPUT_NAME vtildump32)
endif()

# The test of the emulator (tests/emulator_test.cpp): the native runs of the decryption blocks
add_executable(emulator_test tests/emulator_test.cpp third_party/NoVmp/NoVmp/emulator/emulator.cpp third_party/NoVmp/NoVmp/emulator/rwx_allocator.cpp)
target_link_libraries(emulator_test PRIVATE VTIL)
if(MSVC)
    target_compile_options(emulator_test PRIVATE /bigobj /W0 /utf-8 /wd4731)
endif()
