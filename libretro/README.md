# NgpCraft Libretro 0.4

Native Libretro frontend for RetroArch, sharing the desktop C++ sources in `../cpp` and the clean-room HLE BIOS in `../hle_bios`. Python and Qt are not required. No game ROMs or proprietary BIOS images are included.

## Use the Windows x64 core

Copy `prebuilt/windows-x86_64/ngpcraft_libretro.dll` to RetroArch's cores directory and the accompanying `.info` to its info directory. Select **Load Core**, then **Load Content** and your own NGPC ROM. Other operating systems require a build from source.

RetroPad mappings: D-pad for movement, B for NGPC A, A for NGPC B, Start for Option. Native output is 160 x 152 at about 59.95 Hz, with stereo 44.1 kHz audio. Save RAM, save states and rewind are supported. An external BIOS is optional; the clean-room HLE is embedded.

## Build from the repository root

```sh
cmake -S libretro -B libretro/build -DCMAKE_BUILD_TYPE=Release
cmake --build libretro/build --config Release
ctest --test-dir libretro/build -C Release --output-on-failure
```

For cartridge tests, configure with `-DNGPCRAFT_TEST_ROM=/path/to/game.ngp` and optionally `-DNGPCRAFT_TEST_ROM_LINK=/path/to/link_probe.ngc`. Without supplied ROMs, cartridge tests are not registered.

Version 0.4 synchronizes the flash timing model and HLE BIOS with the desktop. The update passed five Libretro tests and 49 flash/save tests. The prebuilt DLL is Windows x64 only.
