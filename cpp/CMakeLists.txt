cmake_minimum_required(VERSION 3.16)
project(ngpc_core LANGUAGES CXX)

# The core is a plain C++17 library with NO dependencies and NO UI. It is consumed
# from Python (ctypes) through a flat C ABI (include/ngpc_core.h).
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(NGPC_CORE_SOURCES
  src/core.cpp
  src/memory.cpp
  src/execute.cpp
  src/mem_family.cpp
  src/reg_family.cpp
  src/z80.cpp
  src/apu.cpp
  src/render.cpp
)

# Shared library: this is what ctypes loads from Python (ngpc_core.{dll,so,dylib}).
add_library(ngpc_core SHARED ${NGPC_CORE_SOURCES})
target_include_directories(ngpc_core PUBLIC include PRIVATE src)
set_target_properties(ngpc_core PROPERTIES PREFIX "")   # ngpc_core.dll, not libngpc_core.dll

# Fold the C++ runtime in statically -- MinGW ONLY. On Windows the MinGW-built
# .dll is loaded by an MSVC-built CPython that knows nothing about the MinGW
# runtime, so without this it trails libgcc/libstdc++/libwinpthread DLL
# dependencies the host cannot resolve. These are MinGW flags, not portable ones:
#   * MSVC on Windows already shares CPython's runtime -- nothing to do.
#   * Linux/macOS load the library against the SAME system C++ runtime the
#     interpreter uses, so normal linking is correct.
#   * AppleClang rejects -static-libgcc outright (it errored the macOS CI build).
# `MINGW` is true only for the MinGW toolchain on Windows, so it scopes all three.
if(MINGW)
  target_link_options(ngpc_core PRIVATE -static-libgcc -static-libstdc++ -static)
endif()
