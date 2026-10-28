cmake_minimum_required(VERSION 3.15)
project(extracmds CXX)

# Path to the x64dbg plugin SDK (the "pluginsdk" folder of an x64dbg release)
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

add_library(extracmds SHARED src/extracmds.cpp)
target_compile_features(extracmds PRIVATE cxx_std_17)
target_include_directories(extracmds PRIVATE "${X64DBG_SDK}")
target_compile_definitions(extracmds PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS UNICODE _UNICODE)
target_link_libraries(extracmds PRIVATE "${X64DBG_SDK}/${ARCH}dbg.lib" "${X64DBG_SDK}/${ARCH}bridge.lib")
set_target_properties(extracmds PROPERTIES SUFFIX "${PLUGIN_EXT}" PREFIX "" OUTPUT_NAME "ExtraCmds")
if(MSVC)
    target_compile_options(extracmds PRIVATE /W4 /utf-8)
endif()

# Optional GUI: the dockable "HvKdFusion" tab. Built only when Qt5 (Widgets) is found and its ABI
# matches the plugin (same arch and MSVC as x64dbg's own Qt 5.12.12). Point CMAKE_PREFIX_PATH at
# x64dbg's Qt, e.g. -DCMAKE_PREFIX_PATH=<repo>/build64/_deps/qt5-src. Qt is 64-bit only here, so the
# 32-bit plugin stays commands-only. Without Qt the plugin still builds, just without the tab.
option(EXTRACMDS_GUI "Build the HvKdFusion GUI tab (needs Qt5 matching x64dbg)" ON)
if(EXTRACMDS_GUI AND ARCH STREQUAL "x64")
    find_package(Qt5 COMPONENTS Core Gui Widgets QUIET)
    if(Qt5_FOUND)
        set_target_properties(extracmds PROPERTIES AUTOMOC ON)
        target_sources(extracmds PRIVATE src/hvkdpanel.cpp)
        target_link_libraries(extracmds PRIVATE Qt5::Core Qt5::Gui Qt5::Widgets)
        target_compile_definitions(extracmds PRIVATE EXTRACMDS_GUI)
        message(STATUS "extracmds: HvKdFusion GUI tab enabled (Qt5 ${Qt5_VERSION})")
    else()
        message(STATUS "extracmds: Qt5 not found, building commands-only (no GUI tab)")
    endif()
endif()
