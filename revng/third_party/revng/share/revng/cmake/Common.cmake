#
# This file is distributed under the MIT License. See LICENSE.md for details.
#

add_custom_target(revng-all-binaries)

macro(prepend_target_property TARGET PROPERTY VALUE SEPARATOR)
  get_target_property(TMP "${TARGET}" "${PROPERTY}")
  if("${TMP}" STREQUAL "TMP-NOTFOUND")
    set(TMP "")
  endif()
  if(NOT "${TMP}" STREQUAL "")
    set(TMP "${SEPARATOR}${TMP}")
  endif()
  set_target_properties("${TARGET}" PROPERTIES "${PROPERTY}" "${VALUE}${TMP}")
endmacro()

macro(append_target_property TARGET PROPERTY VALUE SEPARATOR)
  get_target_property(TMP "${TARGET}" "${PROPERTY}")
  if("${TMP}" STREQUAL "TMP-NOTFOUND")
    set(TMP "")
  endif()
  if(NOT "${TMP}" STREQUAL "")
    set(TMP "${TMP}${SEPARATOR}")
  endif()
  set_target_properties("${TARGET}" PROPERTIES "${PROPERTY}" "${TMP}${VALUE}")
endmacro()

# ~~~
# Fathom, Windows: rev.ng's libraries are shared libraries (.so) loaded as
# plugins (Python, revng opt), which would each carry their own copy of LLVM,
# MLIR and clang in DLLs (separate registries, separate MLIR TypeIDs), and one
# DLL exporting all of them would exceed the 65535 exports of a PE file. So
# on Windows every library of rev.ng is an OBJECT library, and each module or
# executable links all of them, with LLVM, MLIR and clang, statically:
# revng_win32_link_all() at the end of the configuration (the Python module
# _pipebox, revng-opt, the tools).
# ~~~
if(WIN32)
  set_property(GLOBAL PROPERTY REVNG_WIN32_LIBRARIES "")
  set_property(GLOBAL PROPERTY REVNG_WIN32_LINK_ALL "")
endif()

function(revng_win32_link_all_later TARGET)
  set_property(GLOBAL APPEND PROPERTY REVNG_WIN32_LINK_ALL "${TARGET}")
endfunction()

# The libraries of rev.ng in dependency order (a library after those it
# uses), into the global property REVNG_WIN32_SORTED
function(revng_win32_visit LIBRARY)
  get_property(VISITED GLOBAL PROPERTY REVNG_WIN32_VISITED)
  if("${LIBRARY}" IN_LIST VISITED)
    return()
  endif()
  set_property(GLOBAL APPEND PROPERTY REVNG_WIN32_VISITED "${LIBRARY}")
  get_property(LIBRARIES GLOBAL PROPERTY REVNG_WIN32_LIBRARIES)
  get_target_property(DEPENDENCIES "${LIBRARY}" LINK_LIBRARIES)
  get_target_property(INTERFACE "${LIBRARY}" INTERFACE_LINK_LIBRARIES)
  foreach(DEPENDENCY ${DEPENDENCIES} ${INTERFACE})
    if("${DEPENDENCY}" IN_LIST LIBRARIES)
      revng_win32_visit("${DEPENDENCY}")
    endif()
  endforeach()
  set_property(GLOBAL APPEND PROPERTY REVNG_WIN32_SORTED "${LIBRARY}")
endfunction()

function(revng_win32_link_all)
  get_property(LIBRARIES GLOBAL PROPERTY REVNG_WIN32_LIBRARIES)
  get_property(TARGETS GLOBAL PROPERTY REVNG_WIN32_LINK_ALL)
  # Static constructors run in the reverse order of the objects in the link
  # with MinGW: link the users of a library before it (the order of static
  # libraries), so that a library is initialized before its users, as the
  # shared libraries are elsewhere (ScopeGraphUtils' helpers use the names of
  # FunctionTags' tags when they are constructed, for instance)
  set_property(GLOBAL PROPERTY REVNG_WIN32_VISITED "")
  set_property(GLOBAL PROPERTY REVNG_WIN32_SORTED "")
  foreach(LIBRARY ${LIBRARIES})
    revng_win32_visit("${LIBRARY}")
  endforeach()
  get_property(SORTED GLOBAL PROPERTY REVNG_WIN32_SORTED)
  list(REVERSE SORTED)
  foreach(TARGET ${TARGETS})
    # the objects of an OBJECT library are linked when it is a direct
    # dependency: all of them, in that order, then the other libraries the
    # target links
    get_target_property(OWN "${TARGET}" LINK_LIBRARIES)
    set(OTHERS "")
    foreach(ITEM ${OWN})
      if(NOT "${ITEM}" IN_LIST LIBRARIES)
        list(APPEND OTHERS "${ITEM}")
      endif()
    endforeach()
    set_property(TARGET "${TARGET}" PROPERTY LINK_LIBRARIES ${SORTED}
                                                            ${OTHERS})
    # a static program: no DLL of the toolchain to ship
    target_link_options("${TARGET}" PRIVATE -static)
  endforeach()
endfunction()

function(revng_register_library NAME EXPORT_NAME)

  add_dependencies(revng-all-binaries "${NAME}")
  if(WIN32)
    # Fathom: OBJECT libraries, nothing to install on their own
    set_property(GLOBAL APPEND PROPERTY REVNG_WIN32_LIBRARIES "${NAME}")
    return()
  endif()
  target_include_directories("${NAME}" INTERFACE $<INSTALL_INTERFACE:include/>)
  prepend_target_property("${NAME}" BUILD_RPATH
                          "\$ORIGIN:\$ORIGIN/revng/analyses" ":")
  if(NOT "${CMAKE_INSTALL_RPATH}" STREQUAL "")
    append_target_property("${NAME}" BUILD_RPATH "${CMAKE_INSTALL_RPATH}" ":")
  endif()

  set_target_properties("${NAME}" PROPERTIES LIBRARY_OUTPUT_DIRECTORY
                                             "${CMAKE_BINARY_DIR}/lib")

  install(
    TARGETS "${NAME}"
    EXPORT "${EXPORT_NAME}"
    LIBRARY DESTINATION lib/
    ARCHIVE DESTINATION lib/)

endfunction()

macro(revng_add_library NAME TYPE EXPORT_NAME)

  if(WIN32)
    if("${TYPE}" MATCHES "^(STATIC|SHARED|MODULE|OBJECT)$")
      add_library("${NAME}" OBJECT ${ARGN})
    else()
      # no type given: TYPE is the first source
      add_library("${NAME}" OBJECT "${TYPE}" ${ARGN})
    endif()
  else()
    add_library("${NAME}" "${TYPE}" ${ARGN})
  endif()
  revng_register_library("${NAME}" "${EXPORT_NAME}")

endmacro()

# Helper macro to create a new library containing analyses to be employed in
# revng-opt
macro(revng_add_analyses_library NAME EXPORT_NAME)

  if(WIN32)
    set(REVNG_ANALYSES_SOURCES ${ARGN})
    list(REMOVE_ITEM REVNG_ANALYSES_SOURCES STATIC SHARED MODULE)
    add_library("${NAME}" OBJECT ${REVNG_ANALYSES_SOURCES})
    revng_register_library("${NAME}" "${EXPORT_NAME}")
  else()
  add_library("${NAME}" SHARED ${ARGN})
  add_dependencies(revng-all-binaries "${NAME}")
  target_include_directories("${NAME}" INTERFACE $<INSTALL_INTERFACE:include/>)
  prepend_target_property("${NAME}" BUILD_RPATH "\$ORIGIN/../../:\$ORIGIN" ":")
  if(NOT "${CMAKE_INSTALL_RPATH}" STREQUAL "")
    append_target_property("${NAME}" BUILD_RPATH "${CMAKE_INSTALL_RPATH}" ":")
  endif()

  set_target_properties(
    "${NAME}" PROPERTIES LIBRARY_OUTPUT_DIRECTORY
                         "${CMAKE_BINARY_DIR}/lib/revng/analyses")

  install(
    TARGETS "${NAME}"
    EXPORT "${EXPORT_NAME}"
    LIBRARY DESTINATION lib/revng/analyses)
  endif()

endmacro()

macro(revng_add_executable_internal NAME TARGET_PATH)
  # Compute how many ../ to get to root
  set(RELATIVE_TO_ROOT "")
  if(NOT "${TARGET_PATH}" STREQUAL "")
    # Normalize TARGET_PATH
    string(REGEX REPLACE "^/+" "" TARGET_PATH "${TARGET_PATH}")
    string(REGEX REPLACE "/+$" "" TARGET_PATH "${TARGET_PATH}")

    # Count slashes
    string(REPLACE "/" "" TARGET_PATH_WITHOUT_SLASHES "${TARGET_PATH}")
    string(LENGTH "${TARGET_PATH}" TARGET_PATH_LENGTH)
    string(LENGTH "${TARGET_PATH_WITHOUT_SLASHES}"
                  TARGET_PATH_WITHOUT_SLASHES_LENGTH)
    math(EXPR DEPTH
         "${TARGET_PATH_LENGTH} - ${TARGET_PATH_WITHOUT_SLASHES_LENGTH}")

    foreach(IGNORE RANGE "${DEPTH}")
      set(RELATIVE_TO_ROOT "${RELATIVE_TO_ROOT}../")
    endforeach()
  endif()

  add_executable("${NAME}" ${ARGN})
  if(WIN32)
    revng_win32_link_all_later("${NAME}")
  else()
    append_target_property("${NAME}" "LINK_FLAGS" "-pie" " ")
  endif()

  add_dependencies(revng-all-binaries "${NAME}")

  # Set BUILD_RPATH
  prepend_target_property(
    "${NAME}"
    BUILD_RPATH
    "\$ORIGIN/${RELATIVE_TO_ROOT}lib/:\$ORIGIN/${RELATIVE_TO_ROOT}lib/revng/analyses/"
    ":")
  if(NOT "${CMAKE_INSTALL_RPATH}" STREQUAL "")
    append_target_property("${NAME}" BUILD_RPATH "${CMAKE_INSTALL_RPATH}" ":")
  endif()

  # Build in the desired directory
  set_target_properties(
    "${NAME}" PROPERTIES RUNTIME_OUTPUT_DIRECTORY
                         "${CMAKE_BINARY_DIR}/${TARGET_PATH}")

endmacro()

macro(revng_add_executable NAME)
  set(TARGET_PATH libexec/revng)
  revng_add_executable_internal("${NAME}" "${TARGET_PATH}" ${ARGN})
  install(TARGETS "${NAME}" RUNTIME DESTINATION "${TARGET_PATH}")
endmacro()

macro(revng_add_test_executable NAME)
  revng_add_executable_internal("${NAME}" "" ${ARGN})
endmacro()

# This macro installs all the files matching the pattern in the extra arguments
macro(install_pattern)

  file(
    GLOB_RECURSE FILES_TO_INSTALL
    RELATIVE "${CMAKE_CURRENT_SOURCE_DIR}"
    ${ARGN})

  file(RELATIVE_PATH RELATIVE_SOURCE_DIR ${CMAKE_SOURCE_DIR}
       ${CMAKE_CURRENT_SOURCE_DIR})

  foreach(FILE ${FILES_TO_INSTALL})
    get_filename_component(INSTALL_PATH "${FILE}" DIRECTORY)
    install(FILES "${FILE}" DESTINATION ${RELATIVE_SOURCE_DIR}/${INSTALL_PATH})
  endforeach(FILE)

endmacro(install_pattern)

# Additional compiler options
include(CheckCXXCompilerFlag)
macro(add_flag_if_available flag)
  string(REPLACE "-" "_" NAME "${flag}")
  string(REPLACE "+" "_" NAME "${NAME}")
  string(REPLACE "=" "_" NAME "${NAME}")
  string(REPLACE "__" "_" NAME "${NAME}")
  string(TOUPPER "${NAME}" NAME)
  check_cxx_compiler_flag("${flag}" IS_SUPPORTED_${NAME})
  if(IS_SUPPORTED_${NAME})
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${flag}")
  endif()
endmacro()

include("${CMAKE_CURRENT_LIST_DIR}/TupleTreeGenerator.cmake")

function(copy_to_build_and_install INSTALL_TYPE DESTINATION)
  foreach(INPUT_FILE ${ARGN})
    make_directory("${CMAKE_BINARY_DIR}/${DESTINATION}")
    configure_file("${INPUT_FILE}" "${CMAKE_BINARY_DIR}/${DESTINATION}"
                   COPYONLY)
    install("${INSTALL_TYPE}" "${INPUT_FILE}" DESTINATION "${DESTINATION}")
  endforeach()
endfunction()

macro(revng_add_test)
  set(options OPTIONAL FAST)
  set(oneValueArgs DESTINATION RENAME)
  set(multiValueArgs TARGETS CONFIGURATIONS)
  cmake_parse_arguments(REVNG_TEST "" "NAME" "" ${ARGN})
  add_test(${ARGN})
  set_tests_properties(
    "${REVNG_TEST_NAME}"
    PROPERTIES ENVIRONMENT
               "REVNG_OPTIONS=--debug-log=verify $ENV{REVNG_OPTIONS}")
endmacro()
