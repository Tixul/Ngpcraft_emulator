# Link cable (serial channel 0) + its debug tools

Purpose:
- describe what the emulated NGPC link cable IS, so the transports and the
  debugger are read against the same model
- specify the read-only observation surface (`ngpc_serial_state`) and the
  Python-side instrumentation (`core/link_debug.py`) the debugger's **Link**
  tab is built on
- state which parts are hardware fidelity and which parts are deliberately
  synthetic (loopback, injection, impairment), so neither is mistaken for the
  other

⚡ **2026-07-25 — the cable no longer needs `bios.bin`.** The COM machinery above the
wire (the `0x10`–`0x1A` system calls, the rings at `0x6C80`/`0x6CC0`, the RX count at
`0x6D01` that SDK code reads directly, and the two serial ISRs) is implemented in the
clean-room BIOS image as well: `hle_bios/README.md`. Two consoles on that image exchange
bytes at the same rate as on the retail BIOS (1500 frames: 3083 vs 3070, three in flight
either way). Two things make or break it, both measured: the serial vectors are
**cross-wired** versus their SDK names — `0x18` receives, `0x19` transmits — and
`COMINIT` must `ei 6`, because the hand-off leaves the CPU at IFF 7 and masks them.
`tests/test_link_cable.py` covers both BIOSes, including an anti-loopback assertion.

Source references:
- `cpp/src/machine.hpp` (`Machine::serial_tick`, port 0xB1 read8), `cpp/src/core.cpp` (ABI)
- `hle_bios/gen_crt0.py` (the clean-room COM driver + serial ISRs)
- `core/link.py` (transports), `core/link_debug.py` (instrumentation), `core/lobby.py`
- `ngpc_debug.py` (the Link tab), `ngpc_shell.py` (`PlayPage._pump_link`)
- project memory: link cable / multiplayer, CFC link stall

## 1. The model

**The cable is a byte pipe between two independent consoles, not a shared
simulation.** Each console runs its own copy of the game; the only thing they
share is the bytes on the wire. There is therefore no determinism requirement,
no lockstep and no rollback — a link is a reliable, ordered byte relay that
honours the hardware handshake. Latency tolerance is naturally high: the BIOS
was written for a 19200 bps partner that may be slow.

Hardware path, per console:

```
game -> BIOS COM routine -> SC0BUF (0x50) write -> [core] one baud-time
     -> transmit FIFO -> HOST RELAY -> peer's receive FIFO
     -> [core] RTS gate (0xB2 bit0) -> SC0BUF read -> INTRX0 -> BIOS RX ring
```

Consequences that matter:

- **Game-agnostic.** The bridge lives at the BIOS COM layer, so every
  link-capable cartridge works with no per-game code.
- **Interrupt vectors are CROSSED on the retail BIOS.** Vector `0x18` FILLS the
  receive ring, `0x19` DRAINS the transmit ring. Raise them by BEHAVIOUR, not by
  the SDK's names.
- ⚡ **0xB1 bit2 is the PEER'S RTS, not "a cable is plugged in"** — MEASURED on two
  real consoles, 2026-08-19, in six physical states
  (`04_MY_PROJECTS/hw_test_link_suite/result/RELEVE_2026-08-19.md`):

  | peer at the other end | bit2 |
  |---|---|
  | nothing · cable with the far end loose · console **switched off** · console **sitting in the BIOS** | **1** = no peer |
  | console running a cartridge that opened its serial port | **0** = peer present |

  A powered console at the BIOS, cable plugged in, **does not register**. The core
  used to derive this from `serial_link_enabled`, i.e. from the host attaching a
  cable, so a game saw a peer where silicon sees none. It now reads
  `serial_cts_high` — the peer's RTS, crossed by the bridge — guarded by
  `serial_cts_seen`, because that flag's default means "peer ready" for the
  transmitter's sake and a lone cabled console would otherwise report a peer
  (`B1=0x02` where silicon reads `0x07`; SNK Gals' Fighter reports a link error on
  exactly that).

  ⛔ **AND SOMEBODY HAS TO SPEAK FOR THE PEER — the online transports did not.**
  `serial_cts_seen` is only set by whoever crosses the two RTS lines, and all three
  places that do it hold BOTH consoles in one process (`InProcessLink._relay`,
  `PlayPage._pump_link`, `ngpc_run_linked`). `TcpLink` and `LobbyLink` have no pin to
  read and set nothing, so from 2026-08-23 to 2026-09-02 **every online console read
  `0xB1 = 0x06`, "no cable", for ever** — Card Fighters stuck on "EITHER PLAYER MUST
  PUSH A", Puyo Pop / Magical Drop / Gals' Fighters "no second player", The Last Blade
  "Link error". Measured with `tools/link_online_probe.py`: online `0x06`, local `0x02`,
  same game, same 400 frames. The transports now call `core.link.declare_peer`, which
  is where the reasoning for asserting it lives. Frozen by
  `tests/test_link_online_detect.py`, contre-exemple included.

  ⚠️ **`ngpc_serial_state.port_b1` must be changed WITH `read8`.** It was left on the
  old model and reported "cable detected" on exactly the sessions that had none, so the
  debugger's Link tab hid the fault it exists to show.

  🔑 Two behaviours come free with the right signal: a merely-powered peer no longer
  registers, and **unplugging mid-session becomes visible to the game**. That is how
  Match of the Millennium raises its own `LINK ERROR` on hardware — the wire itself
  recovers (bit2 goes 0 → 1 → 0 and bytes flow again), but the GAME's state machine
  treats the cut as fatal. Our peer-loss handling was entirely host-side precisely
  because the game could never see the cut.

- Other port bits, same measurement: `B3` bit2 reads 1 on every console (the whole
  byte is `0x04` on one machine and `0x07` on another — a per-CONSOLE constant, not
  the cable, and it does not move when a peer appears); `B6` = `0x05` and `BC` =
  `0xFE` at reset; `SC0CR` bit 7 is a **latch**, `0x00` until any byte has ever
  crossed and `0x80` for ever after, surviving an unplug; `BR0CR` reads back with
  bit 6 set whatever is written (`0x05` → `0x45`).
- **Flow control.** Our RTS = 0xB2 bit0 (0 = ready to receive). The peer's RTS
  drives our CTS0 pin, which halts our transmitter when the game set
  SC0MOD<CTSE>. **CTS gates the START of a byte, never one already going out**
  (datasheet §3.11: "after completion of the current data send"; fig 3.11(16)
  Note 1: "if the CTS signal rises during transmission, the NEXT data is not
  sent"). The core used to re-test CTS every tick and freeze the byte in flight
  — measured at 6001 held ticks inside one frame of a Card Fighters' Clash VS
  exchange. `Machine::serial_tx_shifting` is what remembers that a byte has
  committed.

## 2. Transports

All three present the same interface (`pump()` / `disconnect()` /
`bytes_out` / `bytes_in`) and all three accept an optional monitor:

| transport | where | use |
|---|---|---|
| `core.link.InProcessLink` | two machines in one process | two players on one PC |
| `core.link.TcpLink` | one machine + a socket | LAN / direct host-join |
| `core.lobby.LobbyLink` | one machine + the relay server | online lobby |
| `core.link.FlycastLink` | one machine + a Dreamcast in Flycast (UDP) | NGPC ↔ Dreamcast cable |
| `core.link_debug.LoopbackLink` | one machine, no peer | **debug only** |

The shell's local 2-player relay is inline in `PlayPage._pump_link` rather than
using `InProcessLink`; it carries the same tap.

**A linked frame is relayed every `core.link.CABLE_SLICE` = 400 instructions
(`PlayPage.LINK_SLICE` points at it), and that number is a correctness figure, not a
comfort setting.** The Last Blade's handshake times out on a single frame of latency:
measured on the game's own "message received" byte `0x4B9D`, the console that speaks
first waits from +0 to +6 for the reply and gives up at +6, after which both consoles
show **LINK ERROR**.

| slice | message received | consumed | link driver at +900 |
|---|---|---|---|
| whole frame | player 2 only, +6 | **never** | dead (`0xFF`) → LINK ERROR |
| 2000 instructions | player 2 only, +8 | **never** | dead (`0xFF`) |
| **400 instructions** | **both, +4** | **+6** | **alive (`0x14`)** |
| 100 instructions | both, +4 | +6 | alive (`0x14`) |

An earlier attempt used 2000 and judged on the final screen alone; it failed, and the
conclusion drawn from it — "sub-frame relaying does not help" — was wrong. Do not raise
the slice without re-running that table. Slicing is not free (a few percent of host
time, plus the speed table above `_flash_overlay`), so a console with **no peer** keeps
the plain one-call-per-frame path. See also [NETPLAY_MIRROR.md §0](NETPLAY_MIRROR.md).

**Relaying mid-frame only fixes ONE direction, and that was not enough.** Each page used
to run its whole frame while the other console stood still: player 2 could consume
player 1's bytes and answer inside its own frame, but player 1 had already finished, so
it saw that answer a frame late — every time. Card Fighters' Clash loses its VS
handshake to exactly that (its packet reader polls the BIOS ring a few dozen times and,
finding nothing, raises its error flag). So **two consoles on one PC are INTERLEAVED**:
`PlayPage._run_frame_interleaved` runs a slice of each in turn, pumping between slices,
and the driven page collects its frame from `_prerun` instead of running it again.

| scheduling | reaches a CFC match (5 arbitrary phase offsets) |
|---|---|
| a whole frame each, then relay | **2 / 5** |
| interleaved by `CABLE_SLICE` | **5 / 5** |

Two consequences that are easy to get wrong:
- **`_frames_due()` is not 1 per tick** — the pacers hand out 0 for a while and then 3.
  Interleaving only the first frame of such a batch freezes the peer for the rest, and
  CFC fails again at a batch of 3. Ready-run frames therefore **queue** (`PRERUN_MAX`).
- **Arming a capture window RESETS the core's log**, so the page that RUNS a frame must
  arm it (`_arm_capture`), and a page that merely collects one must not. Measured when
  this went in: player 2's frame captured 801 writes and player 2's own tick read 0 —
  its watchpoints and access viewer silently dead.

`core.link.run_two_consoles_interleaved` is the same loop, shared with mirror netplay,
which owns two consoles for the same reason.

**And because that loop refuses to drive a paused peer, pause and speed are NOT
per-window.** The refusal is deliberate — a console must stay where the user put it —
but it means a one-sided pause leaves the OTHER console running against a partner that
has stopped answering, until the game's own link protocol times out. It reads as a link
failure, not as a pause. So `PlayPage._link_both()` applies pause, speed and
fast-forward to `_link_peer` too, from either window; the propagation lives there and
nowhere else (the `_apply_*` helpers never mirror in turn, so no loop is possible).
Every path that pauses needs a matching wake-up, and the one that is easy to miss is
`_on_menu_choice` leaving the pause menu for Video/Audio/Controls: it does **not** go
through `close_menu` — it stays paused on purpose and returns via `resume_play`.

🔑 **Mirror netplay is deliberately excluded.** There both consoles run on both PCs from
one shared input stream, and `MirrorSession.step()` returns `"waiting"` until the peer's
input for THIS frame has arrived — so neither PC can run ahead whatever its speed dial
says. Already self-limiting: nothing to propagate, nothing to refuse.

### NGPC ↔ Dreamcast — `FlycastLink` (2026-11-03)

The NGPC ↔ Dreamcast cable plugs serial channel 0 into the Dreamcast's SCIF. Flycast
(2.x) carries that port over UDP with no handshake at all (`core/network/null_modem.h`):
`b"D" + byte` per data byte, `b"B"` for a break. Flycast binds `[network] LocalPort`
(37391) and sends to `[network] server`. Launch it with
`-config "network:BattleCable=yes,network:server=127.0.0.1:42500"`; `FlycastLink`
listens on 42500 and sends to 37391. Shell: 🔗 → *Dreamcast (Flycast)…*.

Flycast's SCIF reports CTS always active, so no flow-control line crosses: the detect
line is asserted when the link is attached, like the online TCP cable. Breaks are
counted (`breaks_in`) and dropped — the NGPC has no break line.

**Validated on 2026-11-03**: SNK vs. Capcom: Match of the Millennium (this core, real
BIOS) sent its Olympic VERSUS points to *Capcom vs. SNK Millennium Fight 2000* (Japan,
NOT the Pro edition) in Flycast 2.7 — the Dreamcast showed 「データの受信を完了しました」.
Path: NGPC つうしん → DCつうしん, Dreamcast SECRET MODE → NEOGEO POCKET. The NGPC's
VS menu sends `0x92` probes on its own; the transfer is three FF-delimited frames each
way (NGPC `FF 00 31 31 00 … FF` opens, the Dreamcast echoes each data frame back).
⚠️ Karin refuses without Olympic points (libretro cheat `1045:FF` = 0x5045), and A must
be pressed only once the Dreamcast shows its download stand-by, or the game reports a
cut cable — that is the game's protocol, as on hardware. Tests: `tests/test_link_flycast.py`.

### Finding the peer — the session above the transport

`TcpLink` is the transport; the rendezvous is a layer above it, in the shell, and that
is where a reported *"direct host/client mode never seems to actually get through"*
actually lived. Three properties it must have, each of which it once lacked:

- **The wait is bounded** (`_NetConnect.HOST_TIMEOUT_S` / `JOIN_TIMEOUT_S`). The host's
  `accept()` had no timeout at all, while the joining side did.
- **The wait is interruptible.** `accept()` waits in `POLL_S` slices on a `select()` so a
  cancel lands within a quarter second, and `cancel()` is reachable from the 🔗 menu and
  from closing the host panel. ⚠️ Guard the **accept** as well as the select: a socket
  closed under a `select()` is reported *readable*, so the accept that follows raises
  `WSAENOTSOCK` — and a cancel the player asked for comes back as an error message.
- **Nothing is refused in silence.** `_start_net` returns a bool and its callers honour
  it: `_mirror_pending` must be armed only after a real start, or an earlier CABLE
  attempt still in flight connects and silently opens a MIRROR session instead.

⚡ **Joining retries** rather than failing on the first `ECONNREFUSED`: being the first of
the two to be ready is the normal case, not an error.

Two lifetime rules that Qt punishes hard:

- **A cancelled worker keeps its Python reference until it has really stopped**
  (`_net_retiring`). Dropping the last reference to a running `QThread` destroys the C++
  object under the OS thread still inside it, and `~QThread` answers "Destroyed while
  thread is still running" then `terminate()` — the same hard death the root `conftest.py`
  documents. `cancel()` is not instantaneous: a blocking connect holds for its slice.
- **Signals are identified by their sender, not by a flag.** `connected` is queued, so one
  emitted just before a cancel is delivered *after* it — and both `QMessageBox.question`
  and `QMenu.exec` run nested event loops in which it can land mid-decision.
  `_from_current_worker()` compares `sender()` to `_net_thread`. A boolean "abandoned"
  flag does **not** work: the next attempt resets it, and the cancelled worker gets in.

Covered by `tests/test_link_direct_connect.py` (31 tests).

⚠️ `TcpLink` writes with `send()` plus a pending buffer, **never `sendall()`**: on a
non-blocking socket `sendall` raises `BlockingIOError` the moment the kernel buffer
fills and does not say how much it already handed over, so the old code dropped a whole
write mid-exchange and the peer waited for a packet that was never sent.
`core/lobby.py` had the same fault and was fixed there first.

**There is a second online mode.** Relaying the cable means the game waits for the
network and slows down with it (0.56x speed at a 67 ms round trip, measured). Mirror
netplay runs BOTH consoles on each PC and sends only the controller bytes, so the
cable is local and the latency is spent on input delay instead —
see [NETPLAY_MIRROR.md](NETPLAY_MIRROR.md). Both modes ship; they are mutually
exclusive at runtime (`Shell._one_link_at_a_time`), and **both are reachable from the
lobby**: a room advertises which one it is for (`mode`), so the joiner starts the same.

**How much network latency a cable-relayed game survives is the GAME's business.**
Measured for CFC's VS handshake with a smooth delay on the wire: fine up to ~61 ms one
way, dead by ~77 ms. ⚠️ Do not measure this with a delay quantised to whole FRAMES —
that delivers in bursts and dies far earlier, which is how "CFC tolerates no latency at
all" was once wrongly concluded.

Delivery into the receive FIFO is **unconditional**. The core's `serial_tick` is
the authoritative flow-control gate (it only PRESENTS a byte once our RTS is
low), so holding bytes back in the host can strand a handshake byte and read as
"no cable".

## 2.1 Exchange cadence — a per-game observation, NOT a platform constant (2026-08-03)

> 📌 **Read §2.2 first if you want the number that matters.** This section chased the wrong
> quantity and says so; it is kept because the negative result is worth having. The exchange
> cadence is game behaviour. The *hardware* parameter — the byte time — is in §2.2, and it is
> derived from the datasheet.

Samurai Shodown! 2 and The Last Blade exchange once every 2 frames at idle. **Fatal Fury does
it on every frame.** So there is no platform cadence to conform to: what follows is a set of
per-cartridge measurements, all taken here.

Count **emulated frames, not milliseconds.** The host's speed has nothing to do with
what the game experiences, and games count their give-up timers in ticks.

| game | first bytes | steady state | median gap between exchanges |
|---|---|---|---|
| Samurai Shodown! 2 | `FC 01 00 30` | `F0` ×441 | **2 frames — 99 %** |
| The Last Blade | `FC 02 00 95` | `F0` ×437 | **2 frames — 99 %** |

Both rows are ours, taken from `scratchpad/cadence.py`: two consoles, cable armed at frame 0,
900 frames, the gap measured between successive bursts leaving one console.

`0x0030` in that first frame is the
**software cassette ID code** in Samurai Shodown! 2's own header — `0x200020`, 2 bytes,
little-endian BCD, per the SNK SDK's cassette recognition header format (`MANSysPro.txt`,
"Software cassette recognition header information format"). The Last Blade sends
`FC 02 00 95`; its ID code is `0x0095`, and note its **second byte is `02`, not `01`**.

🧪 **The instrument was controlled.** The relay pumps ~17×/frame (`CABLE_SLICE` = 400
instructions), so a 1- or 3-frame cadence is perfectly measurable — and 1f/4f/6f outliers do
appear in the remaining 1 %. The 2 comes from the game, not from a bench that can only see
even numbers.

### ⚠️ What this does NOT establish — read before quoting it

**It does not validate `kSerialByteCycles`.** Samurai Shodown! 2's idle exchange carries
**one byte per exchange** (441 `F0` bytes for 441 exchanges). One byte at 19200 bps is
520 µs — **1.6 % of the 33.4 ms period**. The cadence is set by the game's VBlank-locked
state machine; the serial byte time is noise inside it. Our baud could be an order of
magnitude wrong and this measurement would still read "2 frames".

So what it really confirms is our **frame rate** and the game's frame-locked link loop.
That is worth having, but it is not a serial-timing proof, and the first version of this
section overstated it.

**What WOULD be baud-sensitive** is a burst: Card Fighters' packets are 0x17–0x19 bytes, so
~26 × 520 µs ≈ 13.5 ms — most of a frame. A timing check with teeth has to measure a burst,
not an idle heartbeat.

**Two more limits.** The `F8` in-match exchange is unmeasured (it needs navigating to VS):
what was compared is one idle stream against another. And a cassette ID code must **never** be
used as a link compatibility gate — Card Fighters' Clash SNK is `0x0067`, Capcom is `0x0068`,
they differ, and those two link (verified here, on both cartridges). A soft warning at most.

### 🔓 Where the "2" comes from — derived in-house, provenance clean

The measurement above says *that* the cadence is 2 frames. This says **why**, from the
cartridge's own code — which is what makes the figure ours to state. (The 16.7 ms frame
period was already ours, from `K2GETechRef.txt`.)

1. ✅ **Derive it from the ROM — DONE for claim (A), 2026-08-03.** The game's link tick is
   driven by the **vertical blank interrupt**, and that is now a fact read out of Tixul's
   own cartridge plus manufacturer documentation, citing nobody.

   The user interrupt vector table lives at `0x6FB8`, and the **Vertical Blanking Interrupt
   slot is `0x6FCC`** (SNK SDK `SysPro.txt`, "Vertical Blanking Interrupt", interrupt
   level 4 — already indexed in `DOC_SOURCES_INDEX.md`). Samurai Shodown! 2 installs
   **`0x200105`** there. Breaking on the instruction that loads SC0BUF and reading the shadow
   call stack gives, outermost first:

   ```
   0x2001A7  ->  0x206C8F     inside the VBlank handler (0x200105), calls the link tick
   0x206CB1  ->  0x206F68     the session's active handler
   0x207107  ->  0x2304CD     the game's COM wrapper
   0x2304DF  ->  0xFF2C0C     the BIOS COM send
   0xFF2C13  ->  0xFF2C17     ... which writes SC0BUF at 0xFF2C36
   ```

   The stack is complete (`depth=5`, `overflow=0`) and its outermost frame is a call made
   **at** `0x2001A7` — so whatever contains `0x2001A7` was entered *without* a CALL, i.e. by
   interrupt dispatch, and the vector says that routine starts at `0x200105`. **The link tick
   hangs off VBlank.**

   ✅ **Claim (B) — "an exchange spans two ticks" — ESTABLISHED, same day.** Method: re-arm a
   narrow write log on the game's RAM (`0x4000-0x6BFF`) **every frame** (re-arming is what
   resets it; reading does not), run exactly one frame, and keep only the writes whose PC
   lies in the link code (`0x206000-0x208000`).

   Over 40 frames the wire shows a perfect alternation — `X.X.X.X…`, 20 of 40 — and **every
   one of the 20 link variables is written on send frames only, never once on a quiet
   frame**:

   | RAM | written by | writes on send / quiet |
   |---|---|---|
   | `0x564D` | `0x206C95`, `0x206F68`, `0x206F75`, `0x20710B` | 20 / **0** |
   | `0x5651`, `0x5652` | `0x20702D`, `0x207077`, `0x20707C` | 20 / **0** |
   | `0x5654`, `0x5656` | `0x207110`, `0x2070CE` | 20 / **0** |
   | `0x565A`, `0x565C`, `0x5721` | `0x206CB8`, `0x206CC2`, `0x206C8F` | 20 / **0** |
   | `0x6BF8`-`0x6BFB` | `0x206CB1`, `0x207794` | 40 / **0** |

   (The RAM window swept is `0x4000-0x6BFF` — "the area usable by the user program, including
   stack area", `MANSysPro.txt`. These addresses and PCs are ours, read out of the write log.
   **They are deliberately left unnamed**: what each byte *means* is not something this
   experiment establishes, and naming them would import an interpretation we have not
   derived. The argument needs only the counts.)

   So the link state machine does its work on **every other** VBlank: one tick sends, the
   next is the round trip through the peer's own tick. **Exchange = 2 ticks = 2 frames.**

   ⇒ **The integer 2 is ours for this cartridge.** It follows from Samurai Shodown! 2's own
   code plus the documented frame rate; no third-party measurement is needed to state it.

   ### 🛑 How far this generalises — it does NOT, and that is the real finding

   Widening the bench past Samurai Shodown! 2 (which is where the third-party work also
   lives, so leaning on it looked derivative even when it was not) produced the opposite of
   confirmation:

   | cartridge | frames carrying traffic, out of 40 |
   |---|---|
   | Samurai Shodown! 2 (idle) | 20 — `X.X.X.…` |
   | The Last Blade (idle) | 20 — `X.X.X.…` |
   | **Fatal Fury — First Contact** | **40 — `XXXX…`** |

   ⇒ **"one exchange every 2 frames" is a property of a particular game's link library in a
   particular state, not a property of the console.** Fatal Fury drives the cable on every
   frame. Treating 2 frames as a platform constant — as the first version of this section
   did — is simply wrong, whoever measured it.

   **So the right conformance target is not a single number.** What this project should
   document, and does below, is the **per-cartridge cadence we measure ourselves**. That also
   settles the provenance question by dissolving it: there is no borrowed constant left to
   depend on, because there is no universal constant to borrow.

   ### (A) settled with a sound test — and it is only *partly* true

   Two earlier tests were false positives and should not be repeated. v1 accepted any chain
   root within `+0x800` of the handler — The Last Blade passed at `+0x546`, where `0x20069F`
   is that game's **main loop**. v2 started the shadow stack *at* the handler entry and
   accepted a non-empty stack — Fatal Fury passed with a root at `0x200151`, *before* its
   handler at `0x200171`, so execution had already left the interrupt. **A non-empty stack
   does not prove containment.**

   The sound test uses the **stack pointer**. An interrupt pushes context, so XSP on handler
   entry is already below its pre-interrupt value and stays at or below it for as long as we
   remain inside; a `reti` pops back above. So `XSP(at the SC0BUF write) <= XSP(at handler
   entry)` **is** containment — no window, no shadow stack (`scratchpad/derive_sp.py`).

   | cartridge | writes inside the VBlank interrupt |
   |---|---|
   | Samurai Shodown! 2 | **11 / 12** trials |
   | The Last Blade | **6 / 12** trials |

   ⇒ **The cable is driven from the VBlank path only part of the time, and how often depends
   on the game.** Samurai Shodown! 2 is nearly always inside it; The Last Blade is inside for
   only half its sends, so it also pushes bytes from outside the interrupt.

   **Conclusion for the whole section:** "an exchange takes 2 frames because the link tick is
   VBlank-locked" is a *story that fits one cartridge*, not a derivation that holds for the
   platform. Combined with Fatal Fury sending on every frame, the honest position is the one
   this section now takes — **measure and document each cartridge; claim no constant.**

   ### The per-cartridge table that replaces the constant — started, incomplete

   `scratchpad/cadence_table.py`: one process per cartridge, savestate where we have one,
   A driven through the real input path, 60 observed frames, screenshots kept.

   | cartridge | frames carrying traffic / 60 | bytes exchanged |
   |---|---|---|
   | **Fatal Fury — First Contact** | **60/60**, gaps all 1 frame | 896/896 |
   | Samurai Shodown! 2 · Last Blade · KOF R-2 · Neo Turf | 0/60 | 84 · 57 · 15 · 65, **before** the window |
   | Gals' Fighters · Match of the Millennium · Puzzle Link 2 | 0/60 | 0 |

   ⚠️ **Those zeros are not failures.** The transport is clean in every row
   (`rx_queued == rx_read`, `sc0cr = 0`) and the counters show traffic really happened during
   the button phase, before observation started. It is the blind-navigation trap again:
   mashing A leaves each cartridge in an unpredictable state. A real table needs
   **screenshot-verified navigation, per game**, up to a state where the link is ACTIVE.

   What it does already show: **cadence depends on the STATE, not only on the game** —
   Samurai Shodown! 2 exchanges every 2 frames in its link idle and not at all on another
   screen. One more reason to claim no constant.

   ### Earlier per-cartridge figures (superseded framing, kept for the record)

   `scratchpad/derive_multi.py` re-runs the whole derivation per cartridge, one process each.

   | cartridge | wire pattern over 40 frames | (A) VBlank linkage |
   |---|---|---|
   | Samurai Shodown! 2 | `X.X.X.…` **20/40** | handler `0x200105`, chain root `0x2001A7` = **+0xA2** — tight |
   | The Last Blade | `X.X.X.…` **20/40** | handler `0x200159`, chain root `0x20069F` = **+0x546** — ⚠️ **not established** |

   **(B) is corroborated independently**: a second, unrelated cartridge shows the identical
   two-frame alternation on the wire, 20 sends out of 40 frames.

   ⚠️ **(A) is NOT yet corroborated, and the harness's own verdict was too generous.** It
   accepted any chain root within `+0x800` of the handler, which passes The Last Blade at
   `+0x546` — but earlier work on this project identified `0x20069F` as that game's **main
   loop**, not its VBlank handler. So The Last Blade's link tick may hang off the main loop
   instead, and the automatic "OK" there is a **false positive of the tolerance window**, not
   a result. Tightening it means finding each handler's real extent (its `reti`) instead of
   guessing a window.

   Cartridges whose link opens only past a menu (Fatal Fury, Gals, KOF R-2, Neo Turf Masters,
   Match of the Millennium) are **out of scope for this bench** — it drives no buttons, and an
   attempt to synthesise them by writing `0x00B0` each frame perturbed the input latch badly
   enough that Samurai Shodown! 2 stopped touching SC0BUF at all. Reaching them needs the
   Shell + savestate path used by `scratchpad/sweep.py`.

   ⚠️ **Residual assumption, stated honestly:** this reads the ROM's behaviour *through our
   own CPU emulation*, so it assumes our control flow is right (it is independently
   exercised by every game that boots and plays). What it does **not** depend on is the
   serial timing we were worried about — one byte is 1.6 % of the period, so `kSerialByteCycles`
   could be an order of magnitude out without moving this result.
2. **Measure it on real silicon with our own probe** — the only route that yields an
   *independent hardware* fact, and the only one that can validate `kSerialByteCycles`, which
   nothing ever has. `04_MY_PROJECTS/hw_test_link_2p/` already exists but measures port bits
   and byte counts, **not time**; it needs a hardware timer (T0/T1, known prescaler) started
   on the SC0BUF write and stopped on INTTX0, printing µs/byte on screen. No logic analyser
   required — the console reports its own answer.

## 2.2 The byte time, DERIVED — and it is the number that actually matters

The cadence in §2.1 turned out to be game behaviour. **This** is the hardware parameter, and
it is fully derivable from documents this project owns plus its own measurements. It also
answers the obvious objection to §2.1: *the console has no per-cartridge table, it does not
guess — so the information must be in the machine.* It is. It is in two registers.

**Where the information lives — and who writes it.** Measured across ten cartridges
(`scratchpad/baud.py`, every write to `0x51`-`0x53` logged with its PC):

| | value | written by |
|---|---|---|
| `SC0MOD` (0x52) | **0x69** (via 0x49) | `0xFF2BC9`, `0xFF2C44` — **the BIOS** |
| `BR0CR` (0x53) | **0x05** | `0xFF2BCF` — **the BIOS** |
| `SC0CR` (0x51) | **0x00** | `0xFF2BCC` — **the BIOS** |

Samurai Shodown! 2, The Last Blade, Fatal Fury, Gals' Fighters, KOF R-2, Neo Turf Masters,
Card Fighters' Clash and Puzzle Link 2 all converge on the same values, and **not one of the
writes comes from cartridge space** — they are all BIOS COM routines. **There is no per-game
serial configuration**, which is exactly why the console needs no table.

**What the bits mean** — TMP95C061 datasheet §3.11, figures 3.11(6), 3.11(8), 3.11(12):

```
SC0MOD = 0x69   SM  = 10  -> UART mode, 8-bit length
                SC  = 01  -> clocked by the baud rate generator
                RXE = 1   -> receive enabled
                CTSE= 1   -> bit 6; hardware CTS, channel 0 only ("isn't in channel 1")
BR0CR  = 0x05   BR0CK = 00 -> baud generator input clock phi-T0 = fc/4
                BR0S  = 5  -> divide by 5
UART mode       a further /16 -- the transmit and receive counters are labelled
                "UART only /16" in the channel-0 block diagram
```

**And the clock, from video timing so it owes the serial section nothing:**
`515 cycles/line x 199 lines x 60 Hz = 6 149 100` ≈ **fc = 6.144 MHz** (K2GETechRef).

```
baud = fc / 4 / 5 / 16 = fc / 320 = 19 200 bps
8N1  = 10 bit-times    -> 10 / 19 200 = 520.83 us per byte
                       -> 520.83 us x 6.144 MHz = 3200 CPU cycles
```

⇒ **`kSerialByteCycles = 3200` is derived, to the cycle.** It was previously justified
backwards ("the documented 19200 bps implies fc") — that circularity is now removed from the
constant's comment in `cpp/src/machine.hpp` as well.

This also independently confirms two things the core already modelled: **CTSE is bit 6 of
SC0MOD and exists only on channel 0**, and **UART framing is 8 bits with 16x oversampling**.

✅ **CLOSED ON SILICON, 2026-08-19.** The probe ran on two real consoles
(`04_MY_PROJECTS/hw_test_link_suite`). Saturated one-way transfer carried **3875 bytes in
128 frames** = 2133 ms, i.e. **551 µs per byte** against the 520.83 µs derived here — the 6 %
sitting on the slow side, where the software feeding the ring can only put it. And
reprogramming `BR0CR` to `0x15` gave **983 bytes against 3963**, a ratio of **4.03** where the
φT0 → φT2 step predicts exactly 4.

⇒ **`kSerialByteCycles = 3200` and `serial_byte_cycles()` are both confirmed by measurement**,
not merely derived. The one thing this project had never checked on hardware, and it holds.

## 2.2b 🧭 THE GAP, DECOMPOSED — two independent axes (2026-08-20)

Put every probe counter in **byte times** (the window is 128 frames = 4099 byte times of
3200 cycles) and the whole file finally reads straight:

| | silicon | here |
|---|---|---|
| one-way stream | **1.03** byte times per byte | 1.10 |
| round trip, roles A/B | **3.74** | 5.35 |
| **CPU** cost of one received byte | **93 µs** | 48.9 |

⚡ **The two axes are independent, and that is measured, not argued.** Dropping the
receiver's double charge moves the round trip (766 → 1285) and changes the CPU cost by
**nothing**: 48.9 → 48.8 µs, on a 120-frame average over thousands of bytes.

- **WIRE AXIS** — the double charge delays delivery. Round trip **43% too slow**; without
  it, **17% too fast**. No CPU effect whatsoever.
- **CPU AXIS** — receiving a byte is **1.9x too cheap**. Untouched by anything shipped so
  far.

🔑 **And 1.9 is exactly what the unit defect predicts** (Toshiba states billed as cycles,
a factor of two — `PERF_TIMING_POLICY.md §9ter`): the receive path is BIOS code. Two
measurements with nothing in common land on the same number.

⇒ **They are fixed together or not at all.** The wire axis speeds the round trip up, the
CPU axis slows it down; treating one alone MOVES the error instead of shrinking it. That
is the whole history of this file since 2026-08-19.

### ✅ The link is FULL DUPLEX on silicon

Test 1 is "SPEED BOTH WAYS" and the console runs it at **3963** bytes a window against
**3818** one way. Both directions at once cost nothing. A half-duplex model was built and
measured (`Machine::rx_blocked_by_tx`, default **false**): throughput collapses to **153**
bytes, 26.8 byte times each. Refuted.

### ⛔ Never model from the A/A pattern

The half-duplex idea came from "silicon gains nothing from two tokens in flight while we
halve". **Both halves were wrong.** That pattern is both consoles left on **role A**, where
**neither echoes** — each mistakes the peer's ping for its own reply. It is not a round
trip at all.

⚠️ The probe ROM says so itself, in a comment written after six runs came back
photographed on role A: *"Role B's TRIPS is 0 when the roles are set correctly and NON-ZERO
when both consoles were left on role A — the only thing that tells the two apart
afterwards."* Read that witness before quoting any round-trip number. With the roles
actually set, our two patterns give 766 and 771 — **the same**, exactly like silicon. The
halving never existed; it was a badly driven run.

## 2.3 End-to-end alignment check (2026-08-03)

Every claim in §2.2 checked against what the core actually does.

| # | Datasheet / derivation | Core | Verdict |
|---|---|---|---|
| 1 | `SC0MOD`, `SC0CR`, `BR0CR` **after reset = 0x00** (fig 3.11(6), 3.11(8)) | fresh machine reads `0x00` on all three | ✅ |
| 2 | **CTSE is bit 6 of SC0MOD**, and exists on **channel 0 only** (fig 3.11(12) shades it "isn't in channel 1") | `mem[0x000052] & 0x40`, `memory.cpp` `serial_tick`; only channel 0 is modelled | ✅ |
| 3 | CTS halts the **next** byte, never one already shifting (§3.11 + fig 3.11(16) Note 1) | `serial_tx_shifting` latches the commit | ✅ |
| 4 | `SC0MOD=0x69`, `BR0CR=0x05` ⇒ **3200 cycles/byte** | `kSerialByteCycles = 3200`, used for **both** TX (`z80.cpp`) and RX (`memory.cpp`) | ✅ |
| 5 | The baud generator's rate is a **function of `BR0CR` and `SC0MOD`** | ⚠️ **the core never reads `BR0CR`** — see below | ⚠️ |

### ✅ Closed: the byte time is now COMPUTED (`Machine::serial_byte_cycles`)

`BR0CR` used to be read in exactly one place — `core.cpp`, and only to *show* it in the
debugger. Nothing in the emulation consulted it, so the core would have kept timing at
19 200 bps whatever a cartridge programmed. It was right by agreement, not by construction.

`memory.cpp` now derives it:

```
SM   = SC0MOD<3:2>   00 -> I/O interface mode (not a UART frame)  -> fall back
                     01/10/11 -> 7 / 8 / 9 data bits -> 9 / 10 / 11 bit-times
SC   = SC0MOD<1:0>   01 -> baud rate generator     10 -> internal clock phi1 (fc/2)
                     00 (timer 2) and 11 (don't care) -> fall back, nothing selects them
tap  = 4 << (2 * BR0CR<5:4>)      // phi-T0 fc/4, phi-T2 fc/16, phi-T8 fc/64, phi-T32 fc/256
div  = BR0CR<3:0>, with 0 meaning 16   // "0001: don't set" per the datasheet
cycles/byte = bits * tap * div * 16    // the last 16 is UART oversampling
```

**Verification of the change:**

| check | result |
|---|---|
| `SC0MOD=0x69`, `BR0CR=0x05` (what the BIOS writes for every cartridge) | **3200 cycles** — identical to the old constant |
| `BR0CR=0x15` (φT2 instead of φT0) | 12800 cycles — 4800 bps, the case that used to be silently wrong |
| link test suites (`test_link_cable/_debug/_play/_peer_loss`) | **39 passed** |
| full link sweep, 12 cases | **byte-for-byte identical** to the pre-change run (66/90, 158/177, 52/53, 140/140, 24/25, 52/66, 506/548, 558/448) |

So it is a no-op on the entire library, and only changes configurations no cartridge selects.

⚠️ **There is no runtime test pinning the ratio, and that is deliberate** — three ways to build
one were tried and all fail for environmental reasons (host writes cannot arm a transmission;
synthetic cartridges wedge in under one byte-time; the real probe ROM eats the queued bytes).
The reasoning is recorded in full at the end of `tests/test_link_cable.py` so it is not
re-attempted the same three ways. A proper test wants a purpose-built probe ROM that programs
`BR0CR` and reports its own timing — the same ROM that would validate the constant on silicon.

## 2.4 These fixes live in TWO core trees — reconciled 2026-08-03

Everything in §2.2/§2.3 was written in `cpp/src/`, the tree the desktop shell compiles into
its DLL. **There is a second tree**, and it is the one Android and RetroArch build:

| tree | path | consumers |
|---|---|---|
| desktop | `cpp/src/` | the Python shell (ctypes DLL) |
| libretro | `NGPC_RAG/04_MY_PROJECTS/Ngpcraft_emulator_lirbreto/core/src/` | **Android** (`ngpcraft.coreDir`) and RetroArch |

They had drifted **in both directions**, silently. Both were wrong about something:

- the libretro tree lacked the **SC0BUF fix**, so `read8` fell through to `mem[0x50]` — where
  the last *transmitted* byte sits — once the pending flag cleared. **Card Fighters' Clash's
  link was broken on Android and RetroArch**; it no longer is. It also lacked
  `serial_byte_cycles()`;
- the desktop tree computed `chip * 44100` in **32 bits** in `apu.cpp`, overflowing past
  `chip > 97 392` — under two frames of audio. Widened to 64, as libretro already had it.

**All 11 files of the two trees are now identical.** Verified: libretro builds (MSVC) and its
smoke test runs a real cartridge (482 frames, `state_hash=F89DFE73`, 642 fps); the desktop
builds warning-free, the suite is **2028 passed / 48 skipped**, and the 12-case link sweep is
**byte-for-byte identical** to the runs before the change.

🎯 **This was the prerequisite for PC ↔ Android play.** Mirror netplay requires both machines
to simulate *identically* — only pad bytes cross the wire — so two different cores cannot play
together: the CRC32 exchanged every 60 frames cuts the session.

⚠️ **The structural problem is not solved.** They agree *today*; nothing detects it if they
drift again, which is exactly how this happened. The options — one tree, a failing drift test,
or a written procedure — are laid out in item **0** of the Android DEVLOG.

## 3. Observation: `ngpc_serial_state` (read-only)

How many bytes crossed is already visible to whoever relays them. What is NOT
visible from Python is **where a byte that is not crossing got stuck**. The core
therefore counts each stage; nothing here feeds back into emulation.

`ngpc_serial_state_t` / `core.native.SerialState`:

- channel: `enabled`, `tx_depth`, `rx_depth`, `tx_busy`, `rx_pending`
- handshake: `cts_high`, `rts_low`, `ctse`, `cts_hold_ticks`, `rts_hold_ticks`
- bytes: `tx_count` (written to SC0BUF) → `wire_count` (shifted out) …
  `rx_queued_count` (pushed at us) → `rx_read_count` (read by the CPU)
- interrupts: `irq_tx_count` (0x19), `irq_rx_count` (0x18)
- registers: `sc0buf`, `sc0cr`, `sc0mod`, `br0cr`, `port_b1`, `port_b2`
  — `port_b1` is presented **as read8 presents it** (detect + sub-battery bits
  forced), not as the raw I/O page byte.

Counters are **per cable session**: `ngpc_serial_set_enabled` zeroes them, so a
reading answers "since this link came up".

The reduction of those counters to one sentence lives in
`DebugWindow._link_verdict_text` and is ordered so that the first test to fire
names the EARLIEST stuck stage: no cable → total silence → held by peer CTS →
held by our own RTS → arrived but no INTRX0 → INTRX0 but never read (and, at
interrupt mask level 6, why: `COMOFFRTS` does `ei 6`) → nothing shifted out →
flowing.

## 3.1 Keeping: `ngpc_link_state` (read **and** write) — ABI 16

`ngpc_serial_state` above is the door for **looking**. It is read-only by design,
and that was the whole hole: a save state was the CPU struct, the aux block and the
memory image, and the cable is in **none of the three**. Its FIFOs, its shift
register and its handshake pins live in `Machine`, so restoring a state taken
mid-transfer was a **no-op on the channel**.

⛔ MEASURED 2026-08-04. Captured at `rx_depth=2`, ran 30 frames to `rx_depth=3`,
restored — every field stayed at the post-30-frame value. And two re-simulations
from the "same" restored state diverged **by one cable byte inside 60 frames**.
That is a desync, not a rounding error. Reachable wherever F2 and the rewind ring
are live in a cabled session (local 2P, direct-IP; mirror refuses them for its own
reason), and a prerequisite for any rollback — rolling back two consoles means
restoring the bytes *in flight between them*.

`ngpc_link_state_t` / `core.native.LinkState`, via `ngpc_get_link_state` /
`ngpc_set_link_state`:

- the channel: `link_enabled`, `tx_busy`, `tx_shifting`, `tx_byte`, `cts_high`,
  `rx_pending`, `rx_byte`, the signed baud countdowns `tx_cycles`/`rx_cycles`
- the FIFOs: `tx_fifo`/`rx_fifo`, `NGPC_LINK_FIFO_MAX` = 1024 bytes each, with
  `tx_len`/`rx_len`
- the §3 counters, so a restore does not leave the Link tab reading somebody
  else's totals

Rules, and each one is load-bearing:

- **its own versioned block, not more fields in `ngpc_aux_state_t`.** That struct
  is the sound CPU, the T6W28 and the timers; the cable is none of those. And
  growing it would have silently shifted the memory image inside **every
  `NGPCST02` save state a player already has**
- `version` + `size` are written by the getter and CHECKED by the setter: a blob
  from another build is REFUSED (`-1`), never half-applied
- **restore it AFTER the memory image**, like the aux block: `SC0MOD`/`BR0CR` live
  in the image and decide how fast a byte shifts
- **`overflow` means the snapshot is inexact.** The in-process bridge drains every
  pump (depth 2–3 with the probe ROM), but a socket bridge hands over whatever a
  network burst delivered, so the receive FIFO has no natural ceiling. The getter
  **clamps and raises `overflow`** rather than truncating in silence — a caller
  that sees it must not pretend otherwise
- **NOT saved, on purpose:** `serial_byte_cycles()` is COMPUTED from `SC0MOD`/`BR0CR`
  (§2.2), which come back with the image. A saved derivation that disagreed with
  its source would be worse than none

The player-facing format is `NGPCST03` — see `SAVESTATE.md` §8b, which also lists
the three readers that must learn each new generation together.

## 3.2 Waking the host: `ngpc_set_serial_break` — ABI 17

A host bridging two machines has to relay bytes, and until now it had no way to
know **when**, so it polled: pump the cable every N instructions, N picked for the
worst known game (400, because The Last Blade breaks past it). That number is cable
time measured in *instructions*, and it is the wrong unit — the core already counts
the real one (§2.2: `serial_tick` knows the exact cycle a byte finishes shifting,
because it computes the byte time from `BR0CR`/`SC0MOD`). It simply never told
anyone.

Armed, `ngpc_run` returns `NGPC_SERIAL_EVENT` (status **42**,
`core.native.STATUS_SERIAL_EVENT`) the moment something crosses:

- a byte finished shifting out and is now in the transmit FIFO, **or**
- this machine's RTS changed — it drives the peer's CTS, and the peer's handshake
  stalls until it is relayed. Card Fighters' Clash lives on this one, and a host
  that only woke on *bytes* would leave the peer waiting against a handshake we
  had already released

The instruction in flight is **completed and counted first**, so the machine is
always left on an instruction boundary: this is a **rendezvous, not a trap**, and
the next `ngpc_run` resumes as if nothing had happened. `serial_event` is cleared
on entry to every `ngpc_run`, so the question is asked fresh each call, and arming
clears it too — the first run afterwards answers only for its own traffic.

⚠️ **OFF by default, and not used by the shell.** Every existing caller keeps its
exact behaviour; the relay still polls. This is the prerequisite for "the pair and
the cable in the core" in `LINK_NETPLAY_STUDY.md` §6, not that step itself.
Covered by `tests/test_link_serial_break.py`.

### ⛔ It does NOT replace `LINK_SLICE`, and that was measured

The obvious next move — arm the break, drop the shell's 400-instruction quota, stop
guessing — is **wrong**, and it is worth writing down because the reasoning for it is
sound right up to the point where it fails. MEASURED 2026-08-14.

`LINK_SLICE` does **two** jobs. The break does one of them:

| | slice | break |
|---|---|---|
| relay the cable promptly | approximate | exact |
| advance BOTH consoles in small steps | guaranteed | **cannot** |

The break fires on *our* transmit and *our* RTS. A console that is quietly computing
emits no event, so it runs to the end of its frame — and the peer has not run at all
meanwhile. Step size per `run()` call, same harness, 120 frames:

```
slice 400    every step 400 instructions,  0 of 4901 steps >= 0.8 frame
break, free  median step 6263 (A WHOLE FRAME), 242 of 484 steps >= 0.8 frame
```

That is exactly the "a frame each, then relay" scheduling that `_run_frame_interleaved`
exists to replace, and which the Card Fighters' Clash bench scores **2 of 5** against
**5 of 5**. Note the trap in the first measurement that suggested this: a probe ROM that
transmits constantly stops constantly, so it *looks* fine — the granularity is a property
of how often the GAME talks, which is not a guarantee at all.

Keeping the cap **and** arming the break is safe and buys nothing: 20 544 FFI calls
against 19 164, the same wall time to two decimals, and cable throughput identical
either way (1.7 bytes/frame on the probe ROM). The relay was never the bottleneck —
two consoles cost 2× by construction, the slicing on top is 6%, and that leaves ~880
emulated fps on a 2026 desktop, 14.7× realtime.

## 3.3 The pair itself: `ngpc_run_linked` — ABI 18

⚡ **BOTH CONSOLES AND THE RELAY, INSIDE THE CORE.** §3.2 gave the core a way to hand back
the moment the cable moves; this gives it the pair. The host used to own the schedule: run
console A for a slice of INSTRUCTIONS, cross the FFI boundary, move the bytes, run console
B for a slice, cross back. An instruction count is not cable time, and §4/L3 of
LINK_NETPLAY_STUDY.md found the same answer in every emulator that shipped a working
serial link (TGB Dual, BizHawk/DualGambatte, mGBA's lockstep): both consoles and the cable
belong in the core, paced by the hardware's own serial clock.

The rule has two halves, and the second one is a CLOCK rather than a count:

1. always advance whichever console is **behind in cycles**, in steps bounded by a
   fraction of the cable's own byte time — `serial_byte_cycles() / 8`, which is 400
   CYCLES where the host-side slice was 400 INSTRUCTIONS, roughly ten times coarser and
   in the wrong unit;
2. and relay the moment either console reports the cable moved (§3.2's break, armed for
   the duration of the call and restored afterwards).

⛔ **NEITHER HALF WORKS ALONE, and §3.2 already recorded why the event does not: it fires
on what WE send, so a console that is quietly computing says nothing and runs to the end
of its frame while its peer has not run at all.** The cycle cap is what makes progress
unconditional; the event is what makes the relay exact.

### The lag is measured WITHIN the call — a bug worth keeping written down

The first version compared the two machines' **lifetime** cycle counts, which looks
equivalent and is not. The two consoles drift apart OUTSIDE this function: the shell runs
one alone whenever its peer is paused, rewinding or already holds queued frames, and a
restored save state moves one wholesale. After that, the console that was "behind" ran its
whole frame first while its peer stood still — precisely the latency the interleaving
exists to remove, reappearing only SOMETIMES, according to how the frame pacer had batched
its work.

📏 **MEASURED**, drifted pair, one linked frame: gap **102 487 cycles** against a frame of
**102 485** — a whole frame, to the cycle. Counting from zero at each call: **77 cycles**,
and the same at every drift tested (0, 1, 5, 20, 100 frames).

🔑 **The player found this before any test did**, and the shape of the report is what
identified it: *"it does not crash in the same place or the same way twice — white screen,
frozen screen, a character select that never ends."* **A deterministic core that fails
differently each run is deciding on something outside itself.**

### ⚠️ Totals cannot measure interleaving — `ngpc_link_pair_max_gap`

The obvious assertion — compare the cycles each console consumed during the call — passes
either way: **a whole frame each, in sequence, costs exactly the same cycles as taking
turns**, while leaving one console frozen throughout the other's frame. The first test
written for this bug was green against it. What discriminates is the widest gap that opens
DURING the call, which is why the core exposes it.

### The relay rule, settled by measurement

`relay_pair` pushes bytes **unconditionally**, and does not gate on the receiver's RTS.
Two rules existed in this project and disagreed: the shell's local two-player path and the
Android front end push unconditionally (serial_tick is the real gate; holding back here
can strand a handshake byte and read to the game as "no cable"), while
`InProcessLink._relay` gates. Project memory recorded that as unsettled, so it was not
inherited: measured on the probe ROM, 300 frames, both rules give `767 / 767` bytes with
the same last byte and the same wire counts. ⚠️ That established **no regression**, not
equivalence — the probe drains constantly, so it never exercised RTS back-pressure.

### ✅ NOW SETTLED, and against gating (2026-08-20)

The question was re-opened for a good reason: on silicon the receiver's RTS drives the
sender's CTS, so a held byte is **never put on the wire**, and when RTS drops the sender
still has to shift it out — a whole byte time this core skips by pushing early. And
`rts_hold_ticks` runs to ~18 000 a round-trip run, so the gate is engaged constantly.

A knob was added to try it (`Machine::relay_gates_on_rts`, default **false**). Measured
against silicon on the round-trip and throughput tests:

| | débit (bytes/window) | byte times per byte | round trip A/B |
|---|---|---|---|
| **silicon** | **3963** | **1.03** | **1095** |
| unconditional (shipped) | 3730 | 1.10 | 766 |
| gated on the receiver's RTS | **2750** | **1.49** | 766 — *unchanged* |

⇒ Gating **costs a third of the throughput and does not move the round trip by one unit**.
Whatever the missing cost is, it is not the byte waiting for the gate to open. The
unconditional rule stays, now on evidence rather than on inheritance.

### Who still uses the host relay, on purpose

- an armed **link monitor** (`core/link_debug.py`) — it records, delays, drops and injects
  bytes from Python, and a relay inside the core cannot call it;
- a **net link** — that relay goes to a socket, not to the console beside it;
- `NGPCRAFT_HOST_RELAY=1` — the A/B switch (`core.link.host_relay_forced`). When a link
  game misbehaves, "does the old scheduling still do it?" must be answerable without a
  rebuild. That is how the regression above was pinned on this change in one test.

## 4. Instrumentation: `core/link_debug.py`

`LinkMonitor` is a per-CONSOLE tap placed in a relay. `on_tx` sees what leaves,
`on_rx` what arrives, both stamped with the host's frame number and kept in a
bounded ring (`dump()` for hex+ASCII, `raw()` for a byte capture). Attaching one
costs a deque append per frame.

Two of its three powers are deliberately **synthetic** and must never be
confused with hardware behaviour:

- **Injection** (`inject`, `deliver_injected`) — a FAKE PEER, not a fake cable.
  Bytes enter the real receive path (RTS gate → SC0BUF → INTRX0 → BIOS ring), so
  a game that reacts proves the receive chain end to end with no second console.
- **Impairment** (`Impairment`: `delay_frames`, `drop`, `cut`) — applied to the
  OUTGOING direction only, at the console that owns the monitor. The emulated
  cable is instant and lossless; a real online session is neither, and this is
  how a game's tolerance is rehearsed. Order is preserved under latency (every
  byte waits the same number of pumps).

`LoopbackLink` plugs a console into itself (`echo`) or into a wire that never
answers (`sink`). It exercises the whole hardware path on one machine; it is not
a peer, and a game expecting a partner's protocol will not be fooled for long.

## 5. Validation

- `tests/test_link_cable.py` — the transport and the hardware path.
- `tests/test_link_debug.py` — the monitor, the impairments, injection,
  loopback, the counters, the verdict logic, and the gate that matters: on the
  full hardware path (real BIOS COM routines, probe ROM) the core's counters and
  the tap's totals must agree — `wire_count == bytes_tx`,
  `rx_queued_count == bytes_rx` — plus a mid-session cut that really stops the
  peer hearing.
- `tests/test_link_play.py` — the shell's own relay, tapped, with split input.
- `tests/test_shell_ui.py` — the Link tab renders and drives its three pokes.

### 5.1 The library sweep (out of tree: `scratchpad/sweep.py`)

Run when anything under the cable changes. **One process per case** — the script
re-executes itself — so a game that crashes cannot poison the rest of the table. A case
passes only when everything delivered was also **consumed** (`rx_queued == rx_read`, both
directions), there was no overrun (`sc0cr == 0`), bytes moved **both ways**, **and the
screenshot was looked at**.

Last run 2026-08-03 — 10 cases clean:

| case | tx p1/p2 | result |
|---|---|---|
| SamSho!2 · Last Blade · Fatal Fury · Gals · KOF R-2 · Neo Turf | 24→177 | clean, both ways |
| Card Fighters SNK+Capcom — **local** | 506/548 | **card duel running** |
| Card Fighters SNK+Capcom — **TCP** (`_net_link`, socketpair) | 558/448 | **card duel running** |
| Card Fighters SNK+SNK — **local** | 506/548 | **card duel running** |
| Match of the Millennium · Puzzle Link 2 | 0/0 | **not a failure** — mashing A starts a 1P game; their link sits behind a VS menu the bench does not reach |

⛔ **Known gaps, so nobody reads this table as "everything is covered":** the **mirror** and
**lobby** transports are not swept (the bench raises rather than silently running the local
path and mislabelling it), Card Fighters' six link modes are not selected individually (the
bench mashes A and *lands* in a mode), and the bench still overrides `_frames_due` on both
pages — whereas in the real shell player 1 is paced by the **audio** clock and player 2 by
the wall clock, through the `_prerun` queue.

⚠️ **Read the screenshots, not the counters.** A byte counter that *stops* does not mean the
link died: Card Fighters' duel is turn-based, so the winning run is the QUIET one, while the
run still chattering at 1731 bytes was merely stuck earlier, in the CHOOSE FIRST PLAYER idle
conversation. This is the mirror image of the older trap ("a rising counter does not prove it
works") and it cost three wrong readings in one session.
