/* apu.cpp — the T6W28. See apu.hpp for who writes to it and how I know.
 *
 * The register decode below mirrors core/apu.py line for line, because that model
 * is the ORACLE: the differential test drives both with the same byte stream and
 * demands the same state. If you change one, change the other, or the test fails
 * -- which is the point.
 */
#include "apu.hpp"

#include <algorithm>
#include <cstring>

namespace ngpc {

void Apu::reset() {
    *this = Apu();
}

int Apu::active_noise_period() const {
    if (noise.period_select < 3) return kNoisePeriods[noise.period_select];
    return noise.period_extra;
}

/* The two ports differ only in WHAT a non-volume command means:
 *   LEFT  -> tone periods (channels 0..2)
 *   RIGHT -> the noise control, and tone-3's period reused as the noise period
 * Volumes work on both, one stereo side each -- that IS the T6W28's stereo. */
void Apu::write(uint8_t data, bool left) {
    uint8_t& latch_reg = left ? latch_left : latch_right;
    const uint8_t latch = (data & 0x80) ? data : latch_reg;
    if (data & 0x80) latch_reg = data;

    const int index = (latch >> 5) & 3;

    if (latch & 0x10) {                                  /* volume */
        const int amp = kApuVolumes[data & 0x0F];
        if (index < 3) {
            if (left) square[index].vol_left = amp;
            else      square[index].vol_right = amp;
        } else {
            if (left) noise.vol_left = amp;
            else      noise.vol_right = amp;
        }
        return;
    }

    if (left) {
        if (index >= 3) return;                          /* no tone-3 on this chip */
        Square& sq = square[index];
        /* The 10-bit period arrives in two bytes and is stored ALREADY SHIFTED
         * LEFT BY FOUR -- i.e. multiplied by the chip's own /16 divider -- which is
         * why `period` is in chip clocks and why the mute threshold is 128 and not
         * 8. Do not "fix" the shift. */
        if (data & 0x80) sq.period = (sq.period & 0x3F00) | ((data << 4) & 0x00FF);
        else             sq.period = (sq.period & 0x00FF) | ((data << 8) & 0x3F00);
        return;
    }

    if (index == 2) {                                    /* the noise's extra period */
        if (data & 0x80)
            noise.period_extra = (noise.period_extra & 0x3F00) | ((data << 4) & 0x00FF);
        else
            noise.period_extra = (noise.period_extra & 0x00FF) | ((data << 8) & 0x3F00);
        return;
    }
    if (index == 3) {                                    /* the noise control */
        noise.period_select = data & 3;
        noise.tap     = (data & 0x04) ? kTapWhite : kTapDisabled;
        noise.shifter = 0x4000;                          /* every reconfig resets it */
    }
}

void Apu::write_left(uint8_t data)  { write(data, true); }
void Apu::write_right(uint8_t data) { write(data, false); }

/* One output sample. The oscillators are advanced a whole sample-period at a time
 * -- never clock by clock: at 3.072 MHz that would be 150 million steps a second
 * at our replay speed, and the emulator would stop being fast.
 *
 * This is a POINT sample, which the spec allows in as many words ("resampler naif")
 * and which aliases. Band-limited synthesis is a later pass, not a silent one. */
void Apu::emit_sample() {
    /* ⚠️ THE SAMPLE STEP IS FRACTIONAL, AND TRUNCATING IT DETUNES THE WHOLE CHIP.
     *
     * 3 072 000 / 44 100 = 69.66 chip clocks per output sample. This used to be an
     * integer divide -- 69 -- so the oscillators advanced 0.95 % too slowly, every
     * note came out 0.95 % sharp, and the audio clock ran at 100.9 % of real time
     * (measured). A drift that small is inaudible as pitch and lethal as timing: it
     * overruns the host's audio buffer a little more every second.
     *
     * Fixed point, 16.16. The remainder is carried, so the error does not accumulate. */
    static constexpr uint32_t kStepFP =
        uint32_t((uint64_t(kApuClockHz) << 16) / kAudioSampleHz);   /* 69.66 in 16.16 */
    step_fp += kStepFP;
    const uint32_t step = step_fp >> 16;
    step_fp &= 0xFFFF;

    int left = 0, right = 0;

    for (Square& sq : square) {
        if (sq.period <= kMinAudiblePeriod || (!sq.vol_left && !sq.vol_right)) continue;
        sq.counter += int(step);
        const int toggles = sq.counter / sq.period;
        sq.counter %= sq.period;
        sq.phase ^= (toggles & 1);
        const int sign = sq.phase ? 1 : -1;
        left  += sign * sq.vol_left;
        right += sign * sq.vol_right;
    }

    if (noise.vol_left || noise.vol_right) {
        const int period = std::max(1, 2 * active_noise_period());
        noise.counter += int(step);
        int steps = noise.counter / period;
        noise.counter %= period;
        /* Bounded: a tiny extra-period could otherwise ask for thousands of LFSR
         * steps per sample. 64 is far more than any real period needs. */
        steps = std::min(steps, 64);
        for (int i = 0; i < steps; ++i) {
            noise.shifter = (((noise.shifter << 14) ^ (noise.shifter << noise.tap)) & 0x4000)
                          | (noise.shifter >> 1);
        }
        const int sign = (noise.shifter & 1) ? -1 : 1;
        left  += sign * noise.vol_left;
        right += sign * noise.vol_right;
    }

    /* ⚡ THE SAMPLED VOICE, SUMMED IN. The DAC bypasses the sound chip entirely -- it is
     * the main CPU driving the speaker directly, one byte at a time -- so it is added to
     * the chip's output, not routed through it. Held between writes: the converter keeps
     * driving the last code it was handed. 0x80 is silence, so a game that never touches
     * the DAC contributes exactly nothing here. */
    left  += (int(dac_left)  - kDacSilence) * kDacGain;
    right += (int(dac_right) - kDacSilence) * kDacGain;

    /* Four channels at 64 each = +-256 worst case; scale to a comfortable
     * headroom rather than clipping the one frame where they all align. */
    const int16_t l = int16_t(std::clamp(left  * 64, -32768, 32767));
    const int16_t r = int16_t(std::clamp(right * 64, -32768, 32767));

    if (produced - drained >= kRingFrames) {   /* the host fell behind */
        ++drained;                             /* drop the OLDEST, and COUNT it */
        ++dropped;
    }
    const uint32_t slot = uint32_t(produced % kRingFrames) * 2;
    ring[slot]     = l;
    ring[slot + 1] = r;
    ++produced;
}

void Apu::tick(uint32_t main_cycles) {
    if (main_cycles == 0) return;

    /* main clock -> chip clock (half), carrying the remainder so the audio clock
     * cannot drift away from the video clock over a long session. */
    main_residue += main_cycles;
    const uint32_t chip = main_residue / 2;
    main_residue %= 2;
    if (chip == 0) return;

    /* How many output samples does `chip` chip-clocks buy? Exactly
     * `chip * 44100 / 3072000`, with the remainder carried -- NOT `chip / 69`, which
     * is what this did and which produced 44 522 Hz worth of samples per second while
     * claiming 44 100. */
    chip_residue += chip * kAudioSampleHz;
    uint32_t samples = chip_residue / kApuClockHz;
    chip_residue %= kApuClockHz;

    /* A single `ldir` can bill twenty thousand cycles; a HALT idles a whole frame.
     * Cap the burst so one pathological instruction cannot ask for a million
     * samples in one call -- the ring only holds 16 384 anyway. */
    samples = std::min(samples, Apu::kRingFrames);
    for (uint32_t i = 0; i < samples; ++i) emit_sample();
}

uint32_t Apu::drain(int16_t* out, uint32_t n) {
    const uint64_t available = produced - drained;
    const uint32_t want = uint32_t(std::min<uint64_t>(available, n));
    for (uint32_t i = 0; i < want; ++i) {
        const uint32_t slot = uint32_t((drained + i) % kRingFrames) * 2;
        out[i * 2]     = ring[slot];
        out[i * 2 + 1] = ring[slot + 1];
    }
    drained += want;
    return want;
}

}  // namespace ngpc
