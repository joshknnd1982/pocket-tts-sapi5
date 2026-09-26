cmake_minimum_required(VERSION 3.15)
project(PocketTTSSAPI VERSION 1.2.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)

if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(BUILD_X64 TRUE)
    message(STATUS "Building x64 version")
else()
    set(BUILD_X64 FALSE)
    message(STATUS "Building x86 version")
endif()

include(FetchContent)

FetchContent_Declare(
    sonic
    GIT_REPOSITORY https://github.com/waywardgeek/sonic.git
    GIT_TAG master
)
FetchContent_GetProperties(sonic)
if(NOT sonic_POPULATED)
    FetchContent_Populate(sonic)
endif()

add_library(sonic STATIC
    ${sonic_SOURCE_DIR}/sonic.c
)
target_include_directories(sonic PUBLIC ${sonic_SOURCE_DIR})

add_library(host_client STATIC
    src/host_client.cpp
    src/voice_store.cpp
    src/audio_post.cpp
)

target_include_directories(host_client PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_link_libraries(host_client PUBLIC
    sonic
    ws2_32
    shlwapi
    ole32
)

target_compile_definitions(host_client PRIVATE
    _CRT_SECURE_NO_WARNINGS
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    UNICODE
    _UNICODE
)

add_library(PocketTTSSAPI SHARED
    src/sapi_main.cpp
    src/com.cpp
    src/registry.cpp
    src/ISpDataKeyImpl.cpp
    src/IEnumSpObjectTokensImpl.cpp
    src/ISpTTSEngineImpl.cpp
    src/voice_token.cpp
    src/pockettts_sapi.def
)

target_include_directories(PocketTTSSAPI PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_link_libraries(PocketTTSSAPI PRIVATE
    host_client
    ole32
    oleaut32
    advapi32
    shell32
)

target_compile_definitions(PocketTTSSAPI PRIVATE
    _CRT_SECURE_NO_WARNINGS
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    UNICODE
    _UNICODE
)

set(ALL_TARGETS sonic host_client PocketTTSSAPI)

if(BUILD_X64)
    add_executable(PocketTTSVoiceManager WIN32
        manager/voice_manager.cpp
        manager/manager.rc
    )

    target_include_directories(PocketTTSVoiceManager PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
        ${CMAKE_CURRENT_SOURCE_DIR}/manager
    )

    target_link_libraries(PocketTTSVoiceManager PRIVATE
        host_client
        comctl32
        comdlg32
        winmm
        ole32
        shell32
    )

    target_compile_definitions(PocketTTSVoiceManager PRIVATE
        _CRT_SECURE_NO_WARNINGS
        WIN32_LEAN_AND_MEAN
        NOMINMAX
        UNICODE
        _UNICODE
    )

    list(APPEND ALL_TARGETS PocketTTSVoiceManager)
endif()

if(MSVC)
    foreach(tgt IN LISTS ALL_TARGETS)
        target_compile_options(${tgt} PRIVATE /W3 /MP)
        set_property(TARGET ${tgt} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    endforeach()
endif()
