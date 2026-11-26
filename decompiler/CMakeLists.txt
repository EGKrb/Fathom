cmake_minimum_required(VERSION 3.20)
project(fathom_decompiler C CXX)

# The Decompiler plugin of Fathom: Ghidra's native decompiler (third_party/ghidra, Apache-2.0) on the code
# of the debuggee. Builds the decompiler library, Ghidra's sleigh compiler, the x86 / x86-64 sleigh
# specifications (.sla, compiled at build time) and the plugin; the specifications go next to the plugin,
# in decompiler\.

set(X64DBG_SDK "$ENV{LOCALAPPDATA}/Microsoft/WinGet/Packages/x64dbg.x64dbg_Microsoft.Winget.Source_8wekyb3d8bbwe/pluginsdk"
    CACHE PATH "x64dbg pluginsdk directory")
if(NOT EXISTS "${X64DBG_SDK}/_plugins.h")
    message(FATAL_ERROR "x64dbg plugin SDK not found in '${X64DBG_SDK}', pass -DX64DBG_SDK=<path to pluginsdk>")
endif()
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(ARCH x64)
    set(PLUGIN_EXT .dp64)
else()
    set(ARCH x32)
    set(PLUGIN_EXT .dp32)
endif()

set(GHIDRA "${CMAKE_CURRENT_SOURCE_DIR}/third_party/ghidra")
set(CPP "${GHIDRA}/decompile/cpp")

# The groups of Ghidra's Makefile (decompile/cpp/Makefile)
set(CORE xml marshal space float address pcoderaw translate opcodes metatype globalcontext)
set(DECCORE capability architecture options graph cover block cast typeop database cpool
    comment stringmanage modelrules fspec action loadimage grammar varnode op type
    variable varmap jumptable emulate emulateutil flow userop expression multiprecision
    funcdata funcdata_block funcdata_op funcdata_varnode unionresolve pcodeinject
    heritage prefersplit rangeutil ruleaction subflow blockaction merge double
    transform constseq bitfield coreaction condexe override dynamic crc32 prettyprint
    printlanguage printc printjava memstate opbehavior paramid signature)
set(SLEIGH sleigh pcodeparse pcodecompile sleighbase slghsymbol
    slghpatexpress slghpattern semantics context slaformat compression filemanage)
set(SLACOMP slgh_compile slghparse slghscan)
# the library parts of EXTRA used here: the sleigh architecture and its p-code injection
set(LIBEXTRA sleigh_arch inject_sleigh libdecomp)
file(GLOB ZLIB_SOURCES "${GHIDRA}/decompile/zlib/*.c")

function(ghidra_sources out)
    set(result)
    foreach(name ${ARGN})
        list(APPEND result "${CPP}/${name}.cc")
    endforeach()
    set(${out} ${result} PARENT_SCOPE)
endfunction()

ghidra_sources(LIB_SOURCES ${CORE} ${DECCORE} ${SLEIGH} ${LIBEXTRA})
ghidra_sources(SLA_SOURCES ${CORE} ${SLEIGH} ${SLACOMP})

set(GHIDRA_DEFINITIONS LOCAL_ZLIB NO_GZIP WINDOWS _WINDOWS _SECURE_SCL=0 _HAS_ITERATOR_DEBUGGING=0
    _CRT_SECURE_NO_DEPRECATE _CRT_NONSTDC_NO_DEPRECATE _CRT_SECURE_NO_WARNINGS)
if(ARCH STREQUAL "x64")
    list(APPEND GHIDRA_DEFINITIONS WIN64 _WIN64)
endif()
set(GHIDRA_OPTIONS /EHsc /bigobj /W0 /utf-8)

add_library(ghidra_zlib STATIC ${ZLIB_SOURCES})
target_compile_definitions(ghidra_zlib PRIVATE ${GHIDRA_DEFINITIONS})
target_compile_options(ghidra_zlib PRIVATE /W0)

# OBJECT: every object is linked (the print languages and architectures register through static objects)
add_library(ghidra_decomp OBJECT ${LIB_SOURCES})
target_compile_features(ghidra_decomp PUBLIC cxx_std_17)
target_include_directories(ghidra_decomp PUBLIC "${CPP}")
target_compile_definitions(ghidra_decomp PUBLIC ${GHIDRA_DEFINITIONS})
target_compile_options(ghidra_decomp PRIVATE ${GHIDRA_OPTIONS})
target_link_libraries(ghidra_decomp PUBLIC ghidra_zlib)

# Ghidra's sleigh compiler, run at build time on the x86 specifications
add_executable(sleigh ${SLA_SOURCES})
target_compile_features(sleigh PRIVATE cxx_std_17)
target_include_directories(sleigh PRIVATE "${CPP}")
target_compile_definitions(sleigh PRIVATE ${GHIDRA_DEFINITIONS})
target_compile_options(sleigh PRIVATE ${GHIDRA_OPTIONS})
target_link_libraries(sleigh PRIVATE ghidra_zlib)

set(LANGUAGES "${GHIDRA}/x86/languages")
set(SPEC_OUT "${CMAKE_CURRENT_BINARY_DIR}/decompiler")
file(GLOB LANGUAGE_FILES "${LANGUAGES}/*.ldefs" "${LANGUAGES}/*.pspec" "${LANGUAGES}/*.cspec")
set(SPEC_FILES)
foreach(file ${LANGUAGE_FILES})
    get_filename_component(name "${file}" NAME)
    add_custom_command(OUTPUT "${SPEC_OUT}/${name}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${file}" "${SPEC_OUT}/${name}"
        DEPENDS "${file}")
    list(APPEND SPEC_FILES "${SPEC_OUT}/${name}")
endforeach()
file(GLOB SLEIGH_INCLUDES "${LANGUAGES}/*.sinc")
foreach(spec x86 x86-64)
    add_custom_command(OUTPUT "${SPEC_OUT}/${spec}.sla"
        COMMAND ${CMAKE_COMMAND} -E make_directory "${SPEC_OUT}"
        COMMAND $<TARGET_FILE:sleigh> "${LANGUAGES}/${spec}.slaspec" "${SPEC_OUT}/${spec}.sla"
        DEPENDS sleigh "${LANGUAGES}/${spec}.slaspec" ${SLEIGH_INCLUDES}
        COMMENT "sleigh ${spec}.slaspec")
    list(APPEND SPEC_FILES "${SPEC_OUT}/${spec}.sla")
endforeach()
add_custom_target(sleigh_specs ALL DEPENDS ${SPEC_FILES})

# The plugin
add_library(decompiler_plugin SHARED src/plugin.cpp)
target_compile_features(decompiler_plugin PRIVATE cxx_std_17)
target_include_directories(decompiler_plugin PRIVATE "${X64DBG_SDK}")
target_compile_definitions(decompiler_plugin PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
target_compile_options(decompiler_plugin PRIVATE /EHsc /W3 /utf-8)
target_link_libraries(decompiler_plugin PRIVATE ghidra_decomp "${X64DBG_SDK}/${ARCH}dbg.lib" "${X64DBG_SDK}/${ARCH}bridge.lib")
set_target_properties(decompiler_plugin PROPERTIES SUFFIX "${PLUGIN_EXT}" PREFIX "" OUTPUT_NAME "Decompiler")
add_dependencies(decompiler_plugin sleigh_specs)

# The test program of tests/test_decompiler.py
add_executable(decompiler_target tests/target.c)
target_compile_options(decompiler_target PRIVATE /O1 /GS-)
