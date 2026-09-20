# Animated GIF capture

Click **GIF** beside the screenshot button in the player toolbar.

- Choose seconds (1–60) or emulated game frames (1–3600).
- Choose 10, 20 or 30 GIF images per second. The game runs at 60 frames/s:
  300 game frames are five seconds, or 100 GIF images at 20 images/s.
- Choose a start delay from 0 to 10 seconds (default: 3; 0 starts immediately).
  The button counts down in real seconds while you position the game. Click it
  again to cancel. Leaving the game also cancels a pending countdown. The delay
  is remembered and is not included in the recorded duration; it is unaffected
  by fast-forward or pause. If paused when it ends, capture awaits new game frames.
- Click Start. The button shows recorded/target game frames. Click it again
  to stop early; reaching the target stops automatically.
- The last duration, unit and frame rate are saved in the application settings.

Capture uses game time: pauses and rewind browsing add no frames; fast-forward
reaches the target sooner without speeding up GIF playback. Fresh frames produced
by single-frame stepping are included. Reset/load-state transitions during capture
appear in the output.

GIFs loop indefinitely, contain no audio, and use the native 160 × 152 pixels,
without display filters or colour profiles. GIF palette conversion may reduce
colours. Files go to the configured screenshot directory (otherwise `screenshots`),
named after the ROM with a timestamp. Add that filename to the website gallery
separately; capture does not publish anything.

Encoding runs in the background. Another capture becomes available once export
finishes. Leaving a game or closing the application finalizes a pending capture
and waits for its export. An empty capture produces no file. Export errors appear
in the player's notification area. Recording is limited to one minute to bound
memory usage (up to about 125 MiB of raw RGB at 30 images/s, plus encoder overhead).

Source installations need `Pillow>=10.0` from `requirements.txt`. Rebuild the
distributed executable to include this feature and its encoder dependency.
