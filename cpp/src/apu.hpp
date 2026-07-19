/* apu.hpp — the T6W28 sound generator.
 *
 * Clean-room, from specs/APU_T6W28.md. The Python model in core/apu.py is the
 * ORACLE: this file must agree with it command for command, and a differential
 * test holds it to that (tests/test_apu_native.py). Nothing here is ported from
 * any third-party emulator.
 *
 * WHO WRITES TO IT, AND WHERE — MEASURED, NOT ASSUMED (DEVLOG pass 209)
 * --------------------------------------------------------------------
 * The sound driver runs on the Z80, and I recorded every write it aimed at the
 * chip across all 73 commercial ROMs before wiring anything:
 *
 *     71 of 73 ROMs drive the chip.
 *     Z80 MEMORY writes land on EXACTLY TWO addresses -- 0x4000 and 0x4001.
 *     Nothing else in the whole 0x4000..0x7FFF window is ever touched.
 *
 * That matches the register map (specs/APU_T6W28.md §2): the chip has a RIGHT
 * port and a LEFT port, and address bit A0 picks between them.
 *
 *     0x4000 -> RIGHT      0x4001 -> LEFT
 *
 * The bytes decode as real T6W28 commands. Metal Slug's first two are `0x88`
 * then `0x0F`: latch channel 0 / period, then the data byte -- a G, near enough.
 * Sonic writes `0x93` to one port and `0x96` to the other in the same breath:
 * the same channel at two different volumes, which is the stereo pan that makes
 * a T6W28 a T6W28 and not an SN76489.
 *
 * ⚠️ WHAT IS **NOT** WIRED, AND WHY. The Z80 also executes `OUT (0xFF), A` --
 * 71 of 73 ROMs do it, and the core used to feed those writes to the chip along
 * with everything else. They are NOT sound. The value increments by EXACTLY ONE
 * every time, once every ~12 545 cycles, which is the rate of the timer-3
 * interrupt that drives the sound CPU: it is a tick counter, not a command
 * stream. Pouring a monotonic ramp into a command port would have produced
 * confident, plausible noise. The writes are still RECORDED (ngpc_get_apu_writes,
 * kind = NGPC_APU_WRITE_PORT) and their destination is still unknown; say so.
 */
#ifndef NGPC_APU_HPP
#define NGPC_APU_HPP

#include <cstdint>

namespace ngpc {

/* --- the clock ------------------------------------------------------------
 * The chip lives in the Z80's address space, so it lives in the Z80's clock
 * domain: half the main CPU's 6.144 MHz.
 *
 * ⚠️ THE ONE THING THIS CANNOT CHECK ITSELF IS THE OCTAVE. A tone's frequency is
 * `clock / (32 * n)`. Metal Slug's first note has n = 248, which at 3.072 MHz is
 * 387 Hz (a G) and at 6.144 MHz is 774 Hz (the same G, an octave up). Both are
 * musical, so the pitch cannot arbitrate between them -- it only rules out the
 * SN76489's 3.579545 MHz, which would put that note at 451 Hz, between notes.
 * Settling the octave takes a hardware capture. It is one constant, here. */
constexpr uint32_t kApuClockHz    = 3072000;   /* = main clock / 2 */
constexpr uint32_t kAudioSampleHz = 44100;

/* Logarithmic attenuation, index 0 = full, 15 = silent. Mirrors core/apu.py. */
constexpr int kApuVolumes[16] = {64, 50, 39, 31, 24, 19, 15, 12, 9, 7, 5, 4, 3, 2, 1, 0};
constexpr int kNoisePeriods[3] = {0x100, 0x200, 0x400};
constexpr int kTapWhite    = 13;
constexpr int kTapDisabled = 16;

/* A square channel is muted below this period: the spec's anti-alias guard, and
 * the same number core/apu.py uses. */
constexpr int kMinAudiblePeriod = 128;

struct Square {
    int vol_left  = 0;
    int vol_right = 0;
    int period    = 0;   /* 14 bits, ALREADY multiplied by 16 (see the latch) */
    int phase     = 0;
    int counter   = 0;
};

struct Noise {
    int shifter       = 0x4000;
    int tap           = kTapWhite;
    int period_select = 0;      /* 0..2 -> kNoisePeriods, 3 -> period_extra */
    int period_extra  = 0;
    int vol_left      = 0;
    int vol_right     = 0;
    int counter       = 0;
};

/* ⚡ THE DAC's AMPLITUDE, RELATIVE TO THE SOUND CHIP.
 *
 * The two feed the same amplifier, and nothing in the SDK states their relative gain.
 * The DAC is an 8-bit converter: 256 codes centred on 0x80, so a swing of +/-128 codes.
 * The chip's own full scale here is four channels at kApuVolumes[0] = 4 x 64 = 256.
 * A gain of 2 puts the DAC's full swing at +/-256 -- exactly the chip's full scale,
 * which is the only ratio that is not an arbitrary choice.
 *
 * ⚠️ UNSOURCED, AND FLAGGED AS SUCH. The ratio above is derived, not measured: it is
 * the one value that makes the DAC's full swing land on the chip's own full scale
 * rather than being picked to taste. The true analogue ratio needs a hardware capture,
 * and this project's own rule is that a constant set by ear is a constant set wrong. */
constexpr int kDacGain = 2;
constexpr int kDacSilence = 0x80;   /* unsigned 8-bit PCM: mid-scale is silence */

struct Apu {
    Square square[3];
    Noise  noise;
    uint8_t latch_left  = 0;    /* (index << 1) | is_volume */
    uint8_t latch_right = 0;

    /* Debug channel enable mask: bit0..2 squares, bit3 noise, bit4 DAC. All on by
     * default. Muting drops a channel from the MIX only -- its oscillator keeps
     * advancing, so un-muting does not jump the phase. */
    uint8_t channel_mask = 0x1F;

    /* The sampled voice. Held between writes (zero-order hold), which is what the
     * converter itself does: it keeps driving the last code it was given. */
    uint8_t dac_left  = kDacSilence;
    uint8_t dac_right = kDacSilence;

    /* main-clock cycles not yet converted into chip clocks, and chip clocks not
     * yet converted into an output sample. Carrying both remainders is what keeps
     * the audio clock from drifting away from the video clock over minutes. */
    uint32_t main_residue = 0;
    uint64_t chip_residue = 0;   /* in chip-clocks x kAudioSampleHz -- see Apu::tick */
    uint32_t step_fp = 0;        /* 16.16 remainder of the 69.66-clock sample step */

    /* The output ring. The host drains it once a frame; if it does not, the OLDEST
     * audio is dropped and `dropped` says how much, because silently overwriting
     * samples is how an emulator ends up "sounding fine" while losing a third of
     * its output. */
    static constexpr uint32_t kRingFrames = 16384;   /* ~370 ms of stereo */
    int16_t  ring[kRingFrames * 2] = {};
    uint64_t produced = 0;      /* total stereo frames ever generated */
    uint64_t drained  = 0;      /* total stereo frames handed to the host */
    uint64_t dropped  = 0;      /* frames the host was too slow to collect */

    void reset();
    void write_left(uint8_t data);
    void write_right(uint8_t data);
    /* One byte of sampled voice, straight from the main CPU. */
    void write_dac(uint8_t data, bool left) { (left ? dac_left : dac_right) = data; }
    /* Advance by `main_cycles` of the MAIN CPU clock, emitting output samples. */
    void tick(uint32_t main_cycles);
    /* Copy up to `n` stereo frames (interleaved L,R) to `out`; returns how many. */
    uint32_t drain(int16_t* out, uint32_t n);

private:
    void emit_sample();
    int  active_noise_period() const;
    void write(uint8_t data, bool left);
};

}  // namespace ngpc

#endif
