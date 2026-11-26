# Extract project name from folder
get_filename_component(PROJECT_NAME ${CMAKE_CURRENT_LIST_DIR} NAME)
string(REPLACE " " "_" PROJECT_NAME "${PROJECT_NAME}")

file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS *.cpp *.hpp)
file(GLOB_RECURSE INCLUDES CONFIGURE_DEPENDS includes/*)

add_library(${PROJECT_NAME} STATIC
    ${SOURCES}
    ${INCLUDES}
)

source_group(TREE ${PROJECT_SOURCE_DIR} FILES ${SOURCES} ${INCLUDES})

target_include_directories(${PROJECT_NAME} PUBLIC includes)
target_link_libraries(${PROJECT_NAME} VTIL-Common VTIL-SymEx VTIL-Architecture)

target_precompile_headers(${PROJECT_NAME} REUSE_FROM VTIL-Common)

if(VTIL_UNITY_BUILD)
    set_target_properties(${PROJECT_NAME} PROPERTIES UNITY_BUILD ON)
endif()
