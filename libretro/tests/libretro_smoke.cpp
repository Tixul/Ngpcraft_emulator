#include "libretro.h"
#include "cable_link.hpp"
#include "ngpc_core.h"

#include <chrono>
#include <cstdarg>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {

uint64_t video_calls = 0;
uint64_t audio_frames = 0;
uint64_t input_calls = 0;
uint32_t video_hash = 2166136261u;
uint16_t pad_mask = 0;
bool dimensions_ok = true;
bool offer_input_bitmasks = true;
bool request_missing_bios = false;
bool require_running = false;
std::string log_output;
std::string system_directory;
/* What the frontend answers for the two machine options. Defaults match the core's own
 * defaults so every existing test keeps asking the same questions it always did. */
std::string option_console = "auto";
std::string option_cart_timing = "enabled";
bool machine_options_only = false;
bool fingerprint_only = false;
bool link_loopback_only = false;
/* The crossplay mode: this host becomes ONE of the two consoles on the link cable, and
 * something else -- the NgpCraft desktop emulator, or another copy of this host -- is
 * the other one. See check_link(). */
std::string option_link = "off";
unsigned link_frames = 900;
int link_pad = -1;
int link_expect = -1;
double link_wait = 20.0;   /* seconds to wait for the peer, playing */

uint32_t hash_bytes(const void* ptr, size_t size) {
    const auto* bytes = static_cast<const uint8_t*>(ptr);
    uint32_t hash = 2166136261u;
    for (size_t i = 0; i < size; ++i) {
        hash ^= bytes[i];
        hash *= 16777619u;
    }
    return hash;
}

void RETRO_CALLCONV log_line(enum retro_log_level, const char* format, ...) {
    char text[2048];
    va_list args;
    va_start(args, format);
    std::vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    log_output += text;
}

bool RETRO_CALLCONV environment(unsigned command, void* data) {
    switch (command) {
        case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
        case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS:
        case RETRO_ENVIRONMENT_SET_VARIABLES:
        case RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME:
            return true;
        case RETRO_ENVIRONMENT_GET_INPUT_BITMASKS:
            return offer_input_bitmasks;
        case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
            if (system_directory.empty()) return false;
            *static_cast<const char**>(data) = system_directory.c_str();
            return true;
        case RETRO_ENVIRONMENT_GET_LOG_INTERFACE:
            static_cast<retro_log_callback*>(data)->log = log_line;
            return true;
        case RETRO_ENVIRONMENT_GET_VARIABLE: {
            auto* var = static_cast<retro_variable*>(data);
            if (std::strcmp(var->key, "ngpcraft_boot_mode") == 0)
                var->value = request_missing_bios ? "bios" : "handoff";
            else if (std::strcmp(var->key, "ngpcraft_language") == 0) var->value = "english";
            else if (std::strcmp(var->key, "ngpcraft_console") == 0)
                var->value = option_console.c_str();
            else if (std::strcmp(var->key, "ngpcraft_cart_timing") == 0)
                var->value = option_cart_timing.c_str();
            else if (std::strcmp(var->key, "ngpcraft_link") == 0)
                var->value = option_link.c_str();
            else var->value = nullptr;
            return var->value != nullptr;
        }
        default:
            return false;
    }
}

void RETRO_CALLCONV video_refresh(const void* data, unsigned width, unsigned height,
                                  size_t pitch) {
    ++video_calls;
    dimensions_ok &= data && width == 160 && height == 152 && pitch == 160 * 4;
    if (data) video_hash = hash_bytes(data, height * pitch);
}

void RETRO_CALLCONV audio_sample(int16_t, int16_t) { ++audio_frames; }

size_t RETRO_CALLCONV audio_batch(const int16_t*, size_t frames) {
    audio_frames += frames;
    return frames;
}

void RETRO_CALLCONV input_poll() {}

int16_t RETRO_CALLCONV input_state(unsigned, unsigned device, unsigned, unsigned id) {
    ++input_calls;
    if (device != RETRO_DEVICE_JOYPAD) return 0;
    if (id == RETRO_DEVICE_ID_JOYPAD_MASK) return static_cast<int16_t>(pad_mask);
    return (pad_mask & (uint16_t(1) << id)) ? 1 : 0;
}

bool read_file(const char* path, std::vector<uint8_t>& out) {
    std::ifstream file(path, std::ios::binary);
    if (!file) return false;
    out.assign(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
    return true;
}

void run_sequence(unsigned frames) {
    for (unsigned i = 0; i < frames; ++i) {
        pad_mask = 0;
        if ((i / 5) & 1) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_RIGHT;
        if ((i % 11) < 3) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_B;
        if (i == 7) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_START;
        retro_run();
    }
}

/* Boot the ROM under one pair of machine options, run a fixed stretch, and return the
 * serialized state. The SAVE_RAM buffer is wiped right after the load so the core rebuilds
 * a clean save from the machine it just reset -- otherwise the previous capture's flash and
 * clock would ride into this one and every capture would differ for reasons that have
 * nothing to do with the option under test. */
std::vector<uint8_t> capture_state(const retro_game_info& game, const char* console,
                                   const char* timing, unsigned frames) {
    option_console = console;
    option_cart_timing = timing;
    log_output.clear();
    if (!retro_load_game(&game)) return {};
    void* const save_data = retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
    const size_t save_size = retro_get_memory_size(RETRO_MEMORY_SAVE_RAM);
    if (save_data && save_size) std::memset(save_data, 0, save_size);
    pad_mask = 0;
    for (unsigned i = 0; i < frames; ++i) retro_run();
    std::vector<uint8_t> state(retro_serialize_size());
    if (state.empty() || !retro_serialize(state.data(), state.size())) state.clear();
    retro_unload_game();
    return state;
}

/* ⚡ THE TWO MACHINE OPTIONS, CHECKED THROUGH THE ABI AND NOTHING ELSE. Both were declared
 * and then never applied for the whole life of the port: the core knew the silicon
 * wait-states and the mono console existed and simply never asked for either, so cart code
 * ran ~2.9x too fast and no monochrome NGP could be selected at all.
 *
 * ⛔ THE SHAPE OF THIS TEST IS THE POINT. Each configuration is captured TWICE, alternating,
 * and the pairs must come back IDENTICAL while the two configurations must DIFFER. Comparing
 * one capture per configuration would also "pass" if the difference came from the clock or
 * from flash carried over between loads -- a green test measuring the wrong thing. Equal
 * pairs prove the captures are clean; unequal configurations then prove the option acts.
 * Put `configure_machine` back to boot mode + language and this fails on the FIRST pair. */
bool check_machine_options(const retro_game_info& game) {
    constexpr unsigned frames = 40;
    const std::vector<uint8_t> timed_1 = capture_state(game, "color", "enabled", frames);
    const std::vector<uint8_t> untimed_1 = capture_state(game, "color", "disabled", frames);
    const std::vector<uint8_t> timed_2 = capture_state(game, "color", "enabled", frames);
    const std::vector<uint8_t> untimed_2 = capture_state(game, "color", "disabled", frames);
    if (timed_1.empty() || untimed_1.empty()) {
        std::fprintf(stderr, "machine-options: a capture failed to boot\n");
        return false;
    }
    if (timed_1 != timed_2 || untimed_1 != untimed_2) {
        std::fprintf(stderr, "machine-options: captures are not reproducible, so nothing "
                             "below would mean anything\n");
        return false;
    }
    if (timed_1 == untimed_1) {
        std::fprintf(stderr, "machine-options: cart wait-states change NOTHING after %u "
                             "frames -- the timing is not reaching the machine\n", frames);
        return false;
    }

    /* The console type is stamped into 0x6F91/0x6F92 by the reset, and the work image in a
     * save state covers 0x0000-0xBFFF, so the two machines cannot serialize alike. This one
     * needs no game behaviour at all: it is the byte a cartridge reads to know where it is. */
    const std::vector<uint8_t> mono = capture_state(game, "mono", "enabled", frames);
    const bool mono_logged = log_output.find("Neo Geo Pocket (monochrome)") != std::string::npos;
    const std::vector<uint8_t> colour = capture_state(game, "color", "enabled", frames);
    const bool colour_logged = log_output.find("console = Neo Geo Pocket Color") != std::string::npos;
    if (mono.empty() || colour.empty() || mono == colour) {
        std::fprintf(stderr, "machine-options: the mono NGP serializes exactly like an "
                             "NGPC -- k1ge_console is not being set before the reset\n");
        return false;
    }
    if (!mono_logged || !colour_logged) {
        std::fprintf(stderr, "machine-options: the core did not report the console it "
                             "selected; log follows:\n%s\n", log_output.c_str());
        return false;
    }
    /* `auto` must land on the colour machine here: either the external BIOS identifies
     * itself as the NGPC's, or there is no dump at all and nothing claims otherwise. */
    const std::vector<uint8_t> automatic = capture_state(game, "auto", "enabled", frames);
    if (automatic != colour) {
        std::fprintf(stderr, "machine-options: `auto` did not resolve to the colour "
                             "console; log follows:\n%s\n", log_output.c_str());
        return false;
    }
    std::printf("machine_options=ok timed!=untimed mono!=colour auto==colour\n");
    return true;
}

/* 🔫 THE ANTI-PIRACY CHECK, OBSERVED THROUGH THE SAVE STATE. Metal Slug 2nd Mission looks
 * for a piece of the retail BIOS's char RAM and, when it is missing, wipes its own magic
 * "MET2" at 0x6A88 and disables shoot and jump -- a game that runs and looks perfect with
 * dead controls. The work image is the LAST block of a state and covers 0x0000-0xBFFF, so
 * those four bytes are readable here with no core introspection at all.
 *
 * This is the test the check deserves: asserting that the core LOGGED a restore would pass
 * on a restore that wrote the wrong bytes to the wrong place. Asserting the GAME's own
 * verdict cannot. */
bool check_bios_fingerprint(const retro_game_info& game) {
    constexpr uint32_t kWorkImageSize = 0x00C000;
    constexpr uint32_t kMagicAddress = 0x6A88;
    const std::vector<uint8_t> state = capture_state(game, "auto", "enabled", 20);
    if (state.size() < kWorkImageSize) {
        std::fprintf(stderr, "fingerprint: no state captured\n");
        return false;
    }
    const uint8_t* magic = state.data() + (state.size() - kWorkImageSize) + kMagicAddress;
    /* The game stores its magic as a 32-bit little-endian word, so the four bytes read
     * back reversed: "MET2" is 32 54 45 4D on the bus. Measured, not assumed. */
    const bool intact = std::memcmp(magic, "2TEM", 4) == 0;
    std::printf("fingerprint magic@%04X=%02X%02X%02X%02X (%s) restore_logged=%s\n",
                kMagicAddress, magic[0], magic[1], magic[2], magic[3],
                intact ? "MET2 intact" : "WIPED",
                log_output.find("char-RAM fingerprint") != std::string::npos ? "yes" : "no");
    if (!intact) {
        std::fprintf(stderr, "fingerprint: the game wiped its own magic -- it decided this "
                             "is a pirate copy, and shoot and jump are now dead\n");
        return false;
    }
    return true;
}

/* 🔌 CROSSPLAY, PROVEN BY THE CARTRIDGE RATHER THAN BY A BYTE COUNTER.
 *
 * `link_probe.ngc` (built from the desktop repo's tests/roms/link_probe_main.c) sends its
 * own controller byte through the BIOS COM routines and records what came back:
 *
 *     g_last_rx  @ 0x400A   (u8)     the last byte received
 *     g_rx_total @ 0x400C   (u16 LE) how many have arrived
 *
 * So the assertion is not "the socket moved some bytes" -- it is that THE GAME RUNNING ON
 * THIS CORE RECEIVED THE OTHER PLAYER'S CONTROLLER BYTE, through the real serial hardware
 * and the real BIOS routines at both ends. A relay that carried bytes to the wrong place,
 * or bytes the CPU never saw, cannot pass this.
 *
 * The peer is whatever is on the other end of the port: the desktop emulator's
 * core.link.TcpLink (that is the crossplay test) or a second copy of this host. */
/* 🔌 THE CABLE, WITHOUT A SECOND EMULATOR -- what CI can run.
 *
 * The crossplay proof (tests/crossplay_desktop.py) puts the NgpCraft desktop emulator at
 * the other end, which is the only way to prove the two front ends really share a wire;
 * it needs that repo, its ROM and its BIOS, so it cannot be a build-time test. This one
 * keeps the cable honest with nothing but a socket: the test host itself plays the peer,
 * and answers every byte with a FIXED one.
 *
 * ⛔ THE FIXED BYTE IS THE WHOLE DESIGN. Echoing what arrives would leave the cartridge
 * holding its OWN controller byte, and a relay wired back to its own console -- the
 * classic loopback bug -- would sail through. Answering 0x5A means the assertion is
 * "the game received a byte that could only have come from outside", which loopback
 * cannot fake. */
constexpr uint8_t kPeerByte = 0x5A;

bool check_link_loopback(const retro_game_info& game) {
    constexpr uint32_t kWorkImageSize = 0x00C000;
    constexpr uint32_t kLastRx = 0x400A;
    constexpr uint32_t kRxTotal = 0x400C;
    if (system_directory.empty()) {
        std::fprintf(stderr, "link-loopback: needs --system-dir for the link config\n");
        return false;
    }
    /* The core reads the port from its config file, so the test writes one -- which
     * exercises load_link_config() too rather than reaching past it. */
    const uint16_t port = 47788;
    {
        std::ofstream cfg(system_directory + "/ngpcraft_link.cfg");
        cfg << "port = " << port << "\n";
    }
    option_link = "host";
    if (!retro_load_game(&game)) {
        std::fprintf(stderr, "link-loopback: the ROM did not load\n");
        return false;
    }
    ngpcraft::socket_startup();
    ngpcraft_socket_t peer = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    bool joined = false;
    for (int attempt = 0; attempt < 200 && !joined; ++attempt) {
        if (connect(peer, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0) joined = true;
        else { pad_mask = 0; retro_run(); }      // the console plays while it waits
    }
    if (!joined) {
        std::fprintf(stderr, "link-loopback: could not reach the core's listener\n");
        return false;
    }
    ngpcraft::set_non_blocking(peer);
    ngpcraft::set_no_delay(peer);
    uint64_t from_core = 0, to_core = 0;
    for (unsigned i = 0; i < link_frames; ++i) {
        pad_mask = 0;
        retro_run();
        uint8_t buffer[512];
        const int got = static_cast<int>(
            recv(peer, reinterpret_cast<char*>(buffer), sizeof(buffer), 0));
        if (got > 0) {
            from_core += static_cast<uint64_t>(got);
            for (int b = 0; b < got; ++b) {
                const uint8_t answer = kPeerByte;
                if (send(peer, reinterpret_cast<const char*>(&answer), 1, 0) == 1)
                    ++to_core;
            }
        }
    }
    std::vector<uint8_t> state(retro_serialize_size());
    const bool read_back = state.size() >= kWorkImageSize &&
                           retro_serialize(state.data(), state.size());
    unsigned last_rx = 0, rx_total = 0;
    /* The link block as the state carries it. state_size() lays the blocks out
     * ... link, rtc, work image; counting back from the END needs no header parsing
     * and cannot drift from the writer. */
    ngpc_link_state_t link{};
    bool link_read = false;
    if (read_back) {
        const uint8_t* ram = state.data() + (state.size() - kWorkImageSize);
        last_rx = ram[kLastRx];
        rx_total = ram[kRxTotal] | (ram[kRxTotal + 1] << 8);
        const size_t tail = kWorkImageSize + sizeof(ngpc_rtc_t)
                          + sizeof(ngpc_link_state_t);
        if (state.size() >= tail) {
            std::memcpy(&link, state.data() + (state.size() - tail),
                        sizeof(ngpc_link_state_t));
            link_read = link.version == NGPC_LINK_STATE_VERSION &&
                        link.size == sizeof(ngpc_link_state_t);
        }
    }
    ngpcraft::close_socket(peer);
    retro_unload_game();
    std::printf("link-loopback from_core=%llu to_core=%llu last_rx=%02X rx_total=%u\n",
                static_cast<unsigned long long>(from_core),
                static_cast<unsigned long long>(to_core), last_rx, rx_total);
    if (!read_back) {
        std::fprintf(stderr, "link-loopback: could not read the machine back\n");
        return false;
    }
    if (from_core == 0) {
        std::fprintf(stderr, "link-loopback: the console transmitted NOTHING -- the "
                             "cable never reached the serial hardware; log follows:\n%s\n",
                     log_output.c_str());
        return false;
    }
    if (rx_total == 0 || last_rx != kPeerByte) {
        std::fprintf(stderr, "link-loopback: the cartridge should be holding the peer's "
                             "%02X; it has %02X after %u receives\n",
                     kPeerByte, last_rx, rx_total);
        return false;
    }
    if (!link_read) {
        std::fprintf(stderr, "link-loopback: the state carries no readable link block\n");
        return false;
    }
    /* THE DETECT LINE, not just the bytes. 0xB1 bit2 only clears once something has
     * SPOKEN FOR THE PEER (cts_seen); relaying traffic does not say it. A relay that
     * carries everything and leaves that line down is perfect on every counter above
     * and still refuses to start a match. The desktop declares it too: if the two ends
     * answer differently, the versus menu opens on one side and not the other, and the
     * cable looks broken from exactly one end. */
    if (!link.cts_seen || link.cts_high) {
        std::fprintf(stderr, "link-loopback: the cable is up, but the game would read NO CONSOLE "
                             "at the other end (cts_seen=%u cts_high=%u)\n",
                     link.cts_seen, link.cts_high);
        return false;
    }
    return true;
}

bool check_link(const retro_game_info& game) {
    constexpr uint32_t kWorkImageSize = 0x00C000;
    constexpr uint32_t kLastRx = 0x400A;
    constexpr uint32_t kRxTotal = 0x400C;
    if (!retro_load_game(&game)) {
        std::fprintf(stderr, "link: the ROM did not load\n");
        return false;
    }
    /* ⚡ WAIT FOR THE OTHER PLAYER, PLAYING. A console with the cable in the socket and
     * nobody at the far end keeps running -- that is the behaviour, not a compromise --
     * so waiting here is done by running frames, exactly as a player sitting on the
     * title screen does. Counting the measured frames from the moment the peer arrives
     * is also what makes the byte counts below mean anything. */
    const auto wait_started = std::chrono::steady_clock::now();
    while (log_output.find("link cable connected") == std::string::npos) {
        if (std::chrono::duration<double>(
                std::chrono::steady_clock::now() - wait_started).count() > link_wait) {
            std::fprintf(stderr, "link: no peer within %.0f s -- log follows:\n%s\n",
                         link_wait, log_output.c_str());
            return false;
        }
        pad_mask = 0;
        retro_run();
    }
    pad_mask = 0;
    for (unsigned i = 0; i < link_frames; ++i) {
        retro_run();
        /* The pad byte the probe transmits is written straight to the controller port by
         * the core, from the RetroPad. Holding a fixed one makes the peer's assertion a
         * single known value instead of a moving target. */
        if (link_pad >= 0) {
            const uint8_t byte = static_cast<uint8_t>(link_pad);
            /* There is no ABI call to poke memory, so the pad goes in the only way a
             * front end has: through the RetroPad, mapped back to the same bits. */
            pad_mask = 0;
            if (byte & 0x01) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_UP;
            if (byte & 0x02) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_DOWN;
            if (byte & 0x04) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_LEFT;
            if (byte & 0x08) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_RIGHT;
            if (byte & 0x10) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_B;
            if (byte & 0x20) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_A;
            if (byte & 0x40) pad_mask |= uint16_t(1) << RETRO_DEVICE_ID_JOYPAD_START;
        }
    }
    std::vector<uint8_t> state(retro_serialize_size());
    if (state.size() < kWorkImageSize || !retro_serialize(state.data(), state.size())) {
        std::fprintf(stderr, "link: could not read the machine back\n");
        return false;
    }
    const uint8_t* ram = state.data() + (state.size() - kWorkImageSize);
    const unsigned last_rx = ram[kLastRx];
    const unsigned rx_total = ram[kRxTotal] | (ram[kRxTotal + 1] << 8);
    std::printf("link mode=%s frames=%u last_rx=%02X rx_total=%u\n",
                option_link.c_str(), link_frames, last_rx, rx_total);
    retro_unload_game();
    if (rx_total == 0) {
        std::fprintf(stderr, "link: the cartridge received NOTHING -- log follows:\n%s\n",
                     log_output.c_str());
        return false;
    }
    if (link_expect >= 0 && last_rx != static_cast<unsigned>(link_expect)) {
        std::fprintf(stderr, "link: expected the peer's byte %02X, the game got %02X\n",
                     link_expect, last_rx);
        return false;
    }
    return true;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s test-rom.ngc [--no-bitmask] [--bios-missing] "
                             "[--require-running] [--system-dir=PATH]\n", argv[0]);
        return 2;
    }
    for (int i = 2; i < argc; ++i) {
        if (std::strcmp(argv[i], "--no-bitmask") == 0) offer_input_bitmasks = false;
        else if (std::strcmp(argv[i], "--bios-missing") == 0) request_missing_bios = true;
        else if (std::strcmp(argv[i], "--require-running") == 0) require_running = true;
        else if (std::strncmp(argv[i], "--system-dir=", 13) == 0)
            system_directory = argv[i] + 13;
        else if (std::strcmp(argv[i], "--machine-options") == 0) machine_options_only = true;
        else if (std::strcmp(argv[i], "--fingerprint") == 0) fingerprint_only = true;
        else if (std::strcmp(argv[i], "--link-loopback") == 0) link_loopback_only = true;
        else if (std::strncmp(argv[i], "--link=", 7) == 0) option_link = argv[i] + 7;
        else if (std::strncmp(argv[i], "--link-frames=", 14) == 0)
            link_frames = static_cast<unsigned>(std::strtoul(argv[i] + 14, nullptr, 10));
        else if (std::strncmp(argv[i], "--link-pad=", 11) == 0)
            link_pad = static_cast<int>(std::strtol(argv[i] + 11, nullptr, 16));
        else if (std::strncmp(argv[i], "--link-wait=", 12) == 0)
            link_wait = std::strtod(argv[i] + 12, nullptr);
        else if (std::strncmp(argv[i], "--link-expect=", 14) == 0)
            link_expect = static_cast<int>(std::strtol(argv[i] + 14, nullptr, 16));
        else {
            std::fprintf(stderr, "unknown option: %s\n", argv[i]);
            return 2;
        }
    }
    std::vector<uint8_t> rom;
    if (!read_file(argv[1], rom) || rom.size() < 0x30) {
        std::fprintf(stderr, "cannot read a valid ROM: %s\n", argv[1]);
        return 2;
    }

    retro_set_environment(environment);
    retro_set_video_refresh(video_refresh);
    retro_set_audio_sample(audio_sample);
    retro_set_audio_sample_batch(audio_batch);
    retro_set_input_poll(input_poll);
    retro_set_input_state(input_state);
    retro_init();

    std::vector<uint8_t> oversized(0x400001, 0xFF);
    retro_game_info oversized_game{nullptr, oversized.data(), oversized.size(), nullptr};
    if (retro_load_game(&oversized_game)) {
        std::fprintf(stderr, "oversized ROM was incorrectly accepted\n");
        return 1;
    }

    retro_system_info system{};
    retro_get_system_info(&system);
    retro_game_info game{argv[1], rom.data(), rom.size(), nullptr};
    if (link_loopback_only) {
        const bool ok = check_link_loopback(game);
        retro_deinit();
        return ok ? 0 : 1;
    }
    if (option_link != "off") {
        const bool ok = check_link(game);
        retro_deinit();
        return ok ? 0 : 1;
    }
    if (machine_options_only || fingerprint_only) {
        const bool ok = machine_options_only ? check_machine_options(game)
                                             : check_bios_fingerprint(game);
        retro_deinit();
        return ok ? 0 : 1;
    }
    if (!retro_load_game(&game)) {
        std::fprintf(stderr, "retro_load_game failed\n");
        retro_deinit();
        return 1;
    }
    const bool expected_bios_selected = system_directory.empty()
        ? log_output.find("built-in clean-room HLE BIOS") != std::string::npos
        : log_output.find("external BIOS loaded") != std::string::npos &&
          log_output.find("built-in clean-room HLE BIOS") == std::string::npos;
    if (!expected_bios_selected) {
        std::fprintf(stderr, "unexpected BIOS selection; log follows:\n%s\n",
                     log_output.c_str());
        return 1;
    }

    const size_t save_size = retro_get_memory_size(RETRO_MEMORY_SAVE_RAM);
    void* const save_data = retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
    if (!save_data || save_size < 0x20000) {
        std::fprintf(stderr, "SAVE_RAM is missing or too small\n");
        return 1;
    }

    /* Simulate a truncated/corrupt .srm loaded by the frontend. The core must
     * replace it with a valid clean save instead of disabling persistence. */
    std::memset(save_data, 0, save_size);
    retro_run();
    if (*static_cast<const uint32_t*>(save_data) == 0) {
        std::fprintf(stderr, "corrupt SAVE_RAM was not repaired\n");
        return 1;
    }
    const size_t state_size = retro_serialize_size();
    std::vector<uint8_t> checkpoint(state_size), first(state_size), second(state_size);
    if (!state_size || !retro_serialize(checkpoint.data(), checkpoint.size())) {
        std::fprintf(stderr, "initial serialization failed\n");
        return 1;
    }
    std::vector<uint8_t> wrong_rom_state = checkpoint;
    wrong_rom_state[12] ^= 0x80;
    if (retro_unserialize(wrong_rom_state.data(), wrong_rom_state.size())) {
        std::fprintf(stderr, "state for another ROM was incorrectly accepted\n");
        return 1;
    }

    run_sequence(90);
    if (!retro_serialize(first.data(), first.size()) ||
        !retro_unserialize(checkpoint.data(), checkpoint.size())) {
        std::fprintf(stderr, "state round-trip failed\n");
        return 1;
    }
    run_sequence(90);
    if (!retro_serialize(second.data(), second.size()) || first != second) {
        std::fprintf(stderr, "non-deterministic state after replay: %08X != %08X\n",
                     hash_bytes(first.data(), first.size()),
                     hash_bytes(second.data(), second.size()));
        return 1;
    }
    retro_reset();
    if (!retro_unserialize(first.data(), first.size()) ||
        !retro_serialize(second.data(), second.size()) || first != second) {
        std::fprintf(stderr, "state did not survive a frontend reset/restore cycle\n");
        return 1;
    }

    constexpr unsigned benchmark_frames = 300;
    const uint64_t audio_before_benchmark = audio_frames;
    const auto before = std::chrono::steady_clock::now();
    pad_mask = 0;
    for (unsigned i = 0; i < benchmark_frames; ++i) retro_run();
    const auto elapsed = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - before).count();
    const uint64_t audio_delta = audio_frames - audio_before_benchmark;
    constexpr double fps = 6144000.0 / (515.0 * 199.0);
    const uint64_t expected_audio = static_cast<uint64_t>(
        std::llround(benchmark_frames * 44100.0 / fps));
    const uint64_t audio_error = audio_delta > expected_audio
        ? audio_delta - expected_audio : expected_audio - audio_delta;

    const uint64_t expected_input_calls = video_calls * (offer_input_bitmasks ? 1 : 7);
    const bool runtime_ok = dimensions_ok &&
        video_calls == 1 + 90 + 90 + benchmark_frames &&
        input_calls == expected_input_calls && audio_frames > 0 &&
        (!require_running || audio_error <= 128);

    const auto* current_save = static_cast<const uint8_t*>(
        retro_get_memory_data(RETRO_MEMORY_SAVE_RAM));
    if (!current_save) {
        std::fprintf(stderr, "SAVE_RAM disappeared during execution\n");
        return 1;
    }
    std::vector<uint8_t> persisted_save(current_save, current_save + save_size);
    retro_unload_game();
    const bool reopened = retro_load_game(&game);
    void* reopened_save = retro_get_memory_data(RETRO_MEMORY_SAVE_RAM);
    bool reopen_ok = reopened && reopened_save &&
                     retro_get_memory_size(RETRO_MEMORY_SAVE_RAM) == save_size;
    if (reopen_ok) {
        std::memcpy(reopened_save, persisted_save.data(), persisted_save.size());
        retro_run();
        reopen_ok = std::memcmp(reopened_save, persisted_save.data(), 4) == 0;
    }
    std::printf("core=%s version=%s video=%llu audio=%llu state=%zu sram=%zu "
                "inputs=%llu audio_300=%llu/%llu state_hash=%08X video_hash=%08X "
                "reopen=%s bench=%.1f_fps\n",
                system.library_name, system.library_version,
                static_cast<unsigned long long>(video_calls),
                static_cast<unsigned long long>(audio_frames), state_size, save_size,
                static_cast<unsigned long long>(input_calls),
                static_cast<unsigned long long>(audio_delta),
                static_cast<unsigned long long>(expected_audio),
                hash_bytes(first.data(), first.size()), video_hash,
                reopen_ok ? "ok" : "failed",
                benchmark_frames / elapsed);

    retro_unload_game();
    retro_deinit();
    return runtime_ok && reopen_ok ? 0 : 1;
}
