# NgpCraft for Android 1.1

The native Android application is distributed as a signed universal APK through this repository's [Releases](https://github.com/Tixul/Ngpcraft_emulator/releases). Version 1.1 is prepared locally; the APK must be attached manually before it is available there.

Android 7.0 or later is required (API 24). The APK includes arm64-v8a, armeabi-v7a and x86_64. Download `NgpCraft-1.1.apk`, open it on the device and allow installation from that source when Android requests it. Install over the previous signed version to retain app data.

Features include a ROM library, touch controls, physical gamepads, display filters, cartridge saves, save states and two-player modes. Supply your own game ROMs; a clean-room HLE BIOS is embedded.

Version 1.1 (versionCode 2) uses the synchronized desktop core and HLE, including asynchronous flash timing. Older builds cannot join its mirror sessions. Installation over version 1.0 and OverRev startup/touch navigation through a race start were checked on an Android 16 x86_64 emulator. ARM execution and a full race were not tested in that check.

The Android Studio project is maintained separately; its sources are not included in this folder. The local APK here is ignored by Git and is ready for manual upload to a GitHub release.
