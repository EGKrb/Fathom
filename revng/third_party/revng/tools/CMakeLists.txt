#
# This file is distributed under the MIT License. See LICENSE.md for details.
#

add_subdirectory(clift-opt)
add_subdirectory(internal)
add_subdirectory(lddtree)
add_subdirectory(merge-llvm)
add_subdirectory(model)
add_subdirectory(pypeline)
if(WIN32)
  add_subdirectory(win32-opt)
endif()
