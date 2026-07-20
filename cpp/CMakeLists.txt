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

# Statically link the C++ runtime so the library carries no external runtime
# dependency (it is loaded by a CPython that knows nothing about the compiler used
# here). GCC/Clang on any OS accept the -static-lib* pair.
if(MINGW OR CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_link_options(ngpc_core PRIVATE -static-libgcc -static-libstdc++)
  # Fully-static (-static) is a Windows/MinGW-only need: it folds in libwinpthread
  # so no libwinpthread-1.dll dependency is left for the MSVC CPython host to
  # resolve. On Linux a fully-static shared lib fights glibc (dlopen, NSS) and on
  # macOS it is unsupported outright -- so restrict it to Windows.
  if(WIN32)
    target_link_options(ngpc_core PRIVATE -static)
  endif()
endif()
