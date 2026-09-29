cmake_minimum_required(VERSION 3.16)
project(ngpcraft_libretro LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(NGPCRAFT_CORE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../cpp" CACHE PATH "Shared desktop core")
set(NGPCRAFT_FIRMWARE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../hle_bios" CACHE PATH "Shared clean-room firmware")

set(NGPC_CORE_SOURCES
    "${NGPCRAFT_CORE_DIR}/src/core.cpp"
    "${NGPCRAFT_CORE_DIR}/src/memory.cpp"
    "${NGPCRAFT_CORE_DIR}/src/execute.cpp"
    "${NGPCRAFT_CORE_DIR}/src/mem_family.cpp"
    "${NGPCRAFT_CORE_DIR}/src/reg_family.cpp"
    "${NGPCRAFT_CORE_DIR}/src/z80.cpp"
    "${NGPCRAFT_CORE_DIR}/src/apu.cpp"
    "${NGPCRAFT_CORE_DIR}/src/render.cpp")

set(NGPCRAFT_HLE_BIOS "${NGPCRAFT_FIRMWARE_DIR}/bios_hle.bin")
set(NGPCRAFT_HLE_HEADER "${CMAKE_CURRENT_BINARY_DIR}/generated/bios_hle_data.hpp")
add_custom_command(
    OUTPUT "${NGPCRAFT_HLE_HEADER}"
    COMMAND "${CMAKE_COMMAND}"
            "-DINPUT=${NGPCRAFT_HLE_BIOS}"
            "-DOUTPUT=${NGPCRAFT_HLE_HEADER}"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/embed_bios.cmake"
    DEPENDS "${NGPCRAFT_HLE_BIOS}" "${CMAKE_CURRENT_SOURCE_DIR}/cmake/embed_bios.cmake"
    COMMENT "Embedding clean-room HLE BIOS"
    VERBATIM)

add_library(ngpcraft_libretro SHARED
    libretro/libretro.cpp
    "${NGPCRAFT_HLE_HEADER}"
    ${NGPC_CORE_SOURCES})

target_include_directories(ngpcraft_libretro PRIVATE
    libretro
    "${CMAKE_CURRENT_BINARY_DIR}/generated"
    "${NGPCRAFT_CORE_DIR}/include"
    "${NGPCRAFT_CORE_DIR}/src")

target_compile_definitions(ngpcraft_libretro PRIVATE NGPC_LIBRETRO=1)

# The link cable is a TCP socket (libretro/cable_link.hpp). Windows needs Winsock named
# explicitly; every other target has the BSD calls in libc.
if(WIN32)
    target_link_libraries(ngpcraft_libretro PRIVATE ws2_32)
endif()

if(MSVC)
    target_compile_options(ngpcraft_libretro PRIVATE /W4 /EHsc)
else()
    target_compile_options(ngpcraft_libretro PRIVATE -Wall -Wextra -Wno-unused-parameter)
    target_compile_options(ngpcraft_libretro PRIVATE $<$<CONFIG:Release>:-fvisibility=hidden>)
endif()

if(MINGW)
    target_link_options(ngpcraft_libretro PRIVATE -static-libgcc -static-libstdc++ -static)
    if(CMAKE_BUILD_TYPE STREQUAL "Release" AND CMAKE_STRIP)
        add_custom_command(TARGET ngpcraft_libretro POST_BUILD
            COMMAND "${CMAKE_STRIP}" --strip-unneeded "$<TARGET_FILE:ngpcraft_libretro>"
            COMMENT "Stripping release libretro core")
    endif()
endif()

set_target_properties(ngpcraft_libretro PROPERTIES
    PREFIX ""
    OUTPUT_NAME "ngpcraft_libretro")

install(TARGETS ngpcraft_libretro
    RUNTIME DESTINATION .
    LIBRARY DESTINATION .
    ARCHIVE DESTINATION .)

include(CTest)
if(BUILD_TESTING)
    add_executable(ngpcraft_libretro_smoke tests/libretro_smoke.cpp)
    # The test host plays the peer on the cable, so it needs the same socket helpers
    # (libretro/cable_link.hpp) and therefore the core header they are written against.
    target_include_directories(ngpcraft_libretro_smoke PRIVATE libretro "${NGPCRAFT_CORE_DIR}/include")
    if(WIN32)
        target_link_libraries(ngpcraft_libretro_smoke PRIVATE ws2_32)
    endif()
    target_link_libraries(ngpcraft_libretro_smoke PRIVATE ngpcraft_libretro)
    if(MSVC)
        target_compile_options(ngpcraft_libretro_smoke PRIVATE /W4)
    else()
        target_compile_options(ngpcraft_libretro_smoke PRIVATE -Wall -Wextra -Wpedantic)
    endif()
    set(NGPCRAFT_TEST_ROM "" CACHE FILEPATH "Optional ROM used by the Libretro smoke test")
    set(NGPCRAFT_TEST_EXTERNAL_BIOS_DIR "" CACHE PATH
        "Optional RetroArch system directory containing a 64 KiB external BIOS")
    # Metal Slug 2nd Mission specifically: it is the game that fingerprints the BIOS's
    # char RAM, so it is the only ROM that can answer whether the restore works.
    set(NGPCRAFT_TEST_ROM_FINGERPRINT "" CACHE FILEPATH
        "Optional Metal Slug 2nd Mission ROM for the BIOS char-RAM fingerprint test")
    if(NGPCRAFT_TEST_ROM_FINGERPRINT AND EXISTS "${NGPCRAFT_TEST_ROM_FINGERPRINT}")
        add_test(NAME libretro_bios_fingerprint
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM_FINGERPRINT}"
                    --fingerprint)
    endif()
    if(NGPCRAFT_TEST_ROM AND EXISTS "${NGPCRAFT_TEST_ROM}")
        add_test(NAME libretro_smoke
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM}" --require-running)
        add_test(NAME libretro_smoke_legacy_input
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM}"
                    --no-bitmask --require-running)
        add_test(NAME libretro_smoke_builtin_hle
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM}"
                    --bios-missing --require-running)
        # The two machine options -- cart wait-states and console type -- which were
        # declared in the core header and never applied by this front end.
        add_test(NAME libretro_machine_options
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM}" --machine-options)
    endif()
    # The link cable, with the test host playing the peer. Needs the probe ROM, which
    # lives in the desktop emulator's repo (tests/roms/link_probe.ngc) -- the cartridge
    # is what proves a byte reached the CPU, so there is no substitute for it.
    set(NGPCRAFT_TEST_ROM_LINK "" CACHE FILEPATH
        "Optional link_probe.ngc for the link-cable test")
    if(NGPCRAFT_TEST_ROM_LINK AND EXISTS "${NGPCRAFT_TEST_ROM_LINK}")
        add_test(NAME libretro_link_cable
            COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM_LINK}" --link-loopback
                    --link-frames=400
                    "--system-dir=${CMAKE_CURRENT_BINARY_DIR}/link-test")
        file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/link-test")
    endif()
    if(NGPCRAFT_TEST_ROM AND EXISTS "${NGPCRAFT_TEST_ROM}")
        if(NGPCRAFT_TEST_EXTERNAL_BIOS_DIR)
            add_test(NAME libretro_smoke_external_bios_priority
                COMMAND ngpcraft_libretro_smoke "${NGPCRAFT_TEST_ROM}"
                        "--system-dir=${NGPCRAFT_TEST_EXTERNAL_BIOS_DIR}"
                        --require-running)
        endif()
    endif()
endif()
