# Set CMake options for dependencies
set(CAPSTONE_BUILD_TESTS OFF CACHE BOOL "")
set(CAPSTONE_BUILD_CSTOOL OFF CACHE BOOL "")
set(CAPSTONE_BUILD_STATIC_RUNTIME OFF CACHE BOOL "")
set(CAPSTONE_BUILD_SHARED OFF CACHE BOOL "")
set(CAPSTONE_INSTALL OFF CACHE BOOL "")
set(BUILD_LIBS_ONLY ON CACHE BOOL "")

# Fetch the repository and execute capstone's CMakeLists.txt
message(STATUS "Fetching capstone (this might take a while)...")
FetchContent_Declare(
    capstone
    GIT_REPOSITORY https://github.com/aquynh/capstone
    GIT_TAG        52c66920fc7bfa15fd9626dfd9f646c698aaa99b
    GIT_SHALLOW    false
)
FetchContent_MakeAvailable(capstone)

# These CMake projects erroneously do not specify INTERFACE include directory, so
# we need to add it ourselves.
# This is a slight hack, FetchContent_GetProperties is supposed to do this for us, but it doesnt.
#
# TODO: Maybe contribute a fix to these upstream projects?
#
# capstone target naming differs by version/toolchain; resolve the first valid one cus lazy
set(CAPSTONE_TARGET "")
foreach(_candidate capstone-static capstone_static capstone)
    if(TARGET ${_candidate})
        set(CAPSTONE_TARGET ${_candidate})
        break()
    endif()
endforeach()

if(NOT CAPSTONE_TARGET)
    message(FATAL_ERROR "Capstone target was not created (checked: capstone-static, capstone_static, capstone)")
endif()

get_target_property(capstone_SOURCE_DIR ${CAPSTONE_TARGET} SOURCE_DIR)
set_property(TARGET ${CAPSTONE_TARGET} PROPERTY INTERFACE_INCLUDE_DIRECTORIES ${capstone_SOURCE_DIR}/include)

# Downgrade C++ standard on these targets since they depend on some removed/deprecated features
set_property(TARGET ${CAPSTONE_TARGET} PROPERTY CXX_STANDARD 11)
set_property(TARGET ${CAPSTONE_TARGET} PROPERTY CXX_STANDARD_REQUIRED ON)

# Keep downstream link line stable across Capstone naming variants.
if(NOT TARGET capstone-static)
    add_library(capstone-static ALIAS ${CAPSTONE_TARGET})
endif()
