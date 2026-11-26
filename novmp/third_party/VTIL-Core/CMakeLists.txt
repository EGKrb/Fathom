# Require at least CMake version 3.16 for FetchContent and target_precompiled_headers
cmake_minimum_required(VERSION 3.16)

# Define the VTIL project
project(VTIL-Core)

# Detect if VTIL-Core is compiled as the root project
set(VTIL_ROOT_PROJECT OFF)
if(CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR)
    set(VTIL_ROOT_PROJECT ON)

    # Enable solution folder support
    set_property(GLOBAL PROPERTY USE_FOLDERS ON)
endif()

option(VTIL_BUILD_TESTS "Build tests" ${VTIL_ROOT_PROJECT})
option(VTIL_UNITY_BUILD "Enable unity build for faster compilation" OFF)
option(VTIL_SANITIZE_THREADS "Enable ThreadSanitizer (Clang/GCC only)" OFF)

# Load the dependencies
set(CMAKE_FOLDER "VTIL-Core/Dependencies")
add_subdirectory(Dependencies)

# Include subprojects
# The dependency chain order is  Common > Architecture + SymEx > Optimizer, so include in that order
#
set(CMAKE_FOLDER "VTIL-Core")
add_subdirectory(VTIL-Common)
add_subdirectory(VTIL-SymEx)
add_subdirectory(VTIL-Architecture)
add_subdirectory(VTIL-Compiler)

# After all other targets are defined, include the VTIL interface target
# Use this target in projects that use VTIL: https://github.com/vtil-project/VTIL-Samples
#
add_subdirectory(VTIL)

# Tests
if(VTIL_BUILD_TESTS)
    add_subdirectory(VTIL-Tests)
    enable_testing()
    add_test(NAME VTIL-Tests COMMAND "$<TARGET_FILE:VTIL-Tests>")
endif()
