#include "libretro.h"
#include "ngpc_core.h"
#include "bios_hle_data.hpp"
#include "cable_link.hpp"

#include <algorithm>
#include <array>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

constexpr unsigned kWidth = NGPC_SCREEN_W;
constexpr unsigned kHeight = NGPC_SCREEN_H;
constexpr double kFps = 6144000.0 / (515.0 * 199.0);
constexpr double kSampleRate = 44100.0;
constexpr uint32_t kRunawayInstructions = 2000000;
constexpr uint32_t kWorkImageSize = 0x00C000;
constexpr uint32_t kBatteryRamSize = 0x003000;
constexpr uint32_t kFlashTailSize = 0x010000;
constexpr uint32_t kCart0Base = 0x200000;
constexpr uint32_t kCart1Base = 0x800000;
constexpr size_t kMaximumRomSize = 0x400000;
constexpr uint32_t kSaveMagic = 0x5253474Eu; // "NGSR" little-endian
constexpr uint32_t kSaveVersion = 1;
constexpr uint32_t kStateMagic = 0x5453474Eu; // "NGST" little-endian
// v2 adds ngpc_link_state_t: v1 states carried the CPU, the aux block, the RTC and the
// image -- but not the link cable, so a state taken mid-transfer restored the wrong
// channel. Measured 2026-08-04; see LINK_NETPLAY_STUDY.md. libretro netplay rides on
// retro_serialize, and an incomplete one does not degrade, it desyncs. The header check
// below REFUSES a v1 state rather than reading it one struct short.
constexpr uint32_t kStateVersion = 3;   /* link state v2: the two buffer stages */

// ⏱️ SILICON-CALIBRATED CART TIMING. A FRESH MACHINE STARTS AT ZERO -- free instruction
// fetch, which is not what the hardware does -- so a front end that never sets these gets
// cart code running ~2.9x too fast: a self-timed game (Cool Boarders, Densha de Go!) shows
// 60fps where silicon shows 30. The values belong to the CALLER, not to the core; these are
// the desktop shell's shipping numbers (ngpc_settings.CART_FETCH_WAIT and friends).
//   fetch = 3   hw_calibration/cpu_calib_v1.ngc
//   data  = 0   cpu_calib_v2 read a random cart byte and a RAM byte at the same cost
//               (CRND 252 == RRND 252): only FETCH is wait-stated. ⚠️ 0 is an assumption
//               v2 does not license -- it proved the two EQUAL, not free. Do not guess a
//               replacement: a curve-fit 5 was shipped once and refuted. See machine.hpp.
//   ldir  = 14  per ITERATION of the byte form
//   ldirw = 18  per iteration of the WORD form, which moves TWO bytes -- a different
//               number, measured on Bomberman's HiColor copier, not a scaling of 14.
// `vram_wait` is deliberately absent: the K2GE throttle is real (cpu_calib_v3, VWR < MEM)
// but its cost per byte is unmeasured, and the core defaults it off rather than guess.
/* ⚠️ The two CALIBRATED numbers of the silicon timing model (2026-08-21). Everything
 * else it arms -- Toshiba states counted as TWO cycles, one fetch wait per 16-BIT WORD,
 * the bus interface unit running ahead behind its 4-byte queue, the two-stage
 * transmitter -- is documented or derived, and lives in ngpc_set_timing_silicon. Keep
 * these equal to CART_FETCH_WAIT / CART_BIOS_WAIT in the desktop's ngpc_settings.py. */
constexpr uint32_t kWordFetchWait = 10;
constexpr uint32_t kBiosFetchWait = 8;

// 🎨 WHICH CONSOLE A BIOS IMAGE IS THE BIOS OF. Each retail dump stamps the machine-type
// byte at 0x6F91 with its own id -- `ld (0x6F91),#`, encoded `F1 91 6F 00 <value>` -- and
// only the colour one ever addresses a K2GE register (0x87E2, encoded `E2 87 00`). A
// cartridge reads 0x6F91 and nothing else to know where it is, so this is the same test
// the games run. Our clean-room HLE image stamps neither byte and stays Unknown.
enum class BiosConsole { Unknown, Colour, Mono };
constexpr uint8_t kBiosStampColour[] = {0xF1, 0x91, 0x6F, 0x00, 0x10};
constexpr uint8_t kBiosStampMono[]   = {0xF1, 0x91, 0x6F, 0x00, 0x00};
constexpr uint8_t kBiosK2geReg[]     = {0xE2, 0x87, 0x00};

struct SaveRam {
    uint32_t magic;
    uint32_t version;
    uint32_t rom_id;
    uint32_t capacity[2];
    ngpc_rtc_t rtc;
    uint8_t battery[kBatteryRamSize];
    uint8_t flash_tail[2][kFlashTailSize];
};

struct StateHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t abi;
    uint32_t rom_id;
};

retro_environment_t environ_cb = nullptr;
retro_video_refresh_t video_cb = nullptr;
retro_audio_sample_t audio_cb = nullptr;
retro_audio_sample_batch_t audio_batch_cb = nullptr;
retro_input_poll_t input_poll_cb = nullptr;
retro_input_state_t input_state_cb = nullptr;
retro_log_printf_t log_cb = nullptr;

ngpc_t* machine = nullptr;
std::vector<uint8_t> rom_image;
std::array<uint16_t, kWidth * kHeight> native_video{};
std::array<uint32_t, kWidth * kHeight> video{};
std::array<int16_t, 4096> audio{};
SaveRam save_ram{};
uint32_t rom_id = 0;
bool save_loaded = false;
enum class BiosKind { None, BuiltInHle, External };
BiosKind bios_kind = BiosKind::None;
BiosConsole bios_console = BiosConsole::Unknown;
bool bios_boot = false;
bool console_mono = false;
bool power_pressed = false;
bool input_bitmasks = false;
unsigned controller_device = RETRO_DEVICE_JOYPAD;
uint8_t last_stop_status = 0xFF;
uint32_t last_stop_pc = 0xFFFFFFFFu;
ngpcraft::CableLink cable;
std::string link_peer_address;
uint16_t link_port = ngpcraft::kDefaultPort;
bool link_announced = false;

void fallback_log(enum retro_log_level level, const char* fmt, ...) {
    (void)level;
    va_list ap;
    va_start(ap, fmt);
    std::vfprintf(stderr, fmt, ap);
    va_end(ap);
}

void log_message(enum retro_log_level level, const char* fmt, ...) {
    char text[1024];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(text, sizeof(text), fmt, ap);
    va_end(ap);
    (log_cb ? log_cb : fallback_log)(level, "%s", text);
}

uint32_t fnv1a(const uint8_t* data, size_t size) {
    uint32_t value = 2166136261u;
    for (size_t i = 0; i < size; ++i) {
        value ^= data[i];
        value *= 16777619u;
    }
    return value;
}

const char* get_variable_value(const char* key) {
    if (!environ_cb) return nullptr;
    retro_variable var{key, nullptr};
    if (!environ_cb(RETRO_ENVIRONMENT_GET_VARIABLE, &var)) return nullptr;
    return var.value;
}

bool get_variable(const char* key, const char* wanted) {
    const char* value = get_variable_value(key);
    return value && std::strcmp(value, wanted) == 0;
}

bool contains_bytes(const uint8_t* data, size_t size, const uint8_t* pattern, size_t len) {
    if (size < len) return false;
    return std::search(data, data + size, pattern, pattern + len) != data + size;
}

BiosConsole identify_bios(const uint8_t* data, size_t size) {
    const bool k2ge = contains_bytes(data, size, kBiosK2geReg, sizeof(kBiosK2geReg));
    if (contains_bytes(data, size, kBiosStampColour, sizeof(kBiosStampColour)) && k2ge)
        return BiosConsole::Colour;
    if (contains_bytes(data, size, kBiosStampMono, sizeof(kBiosStampMono)) && !k2ge)
        return BiosConsole::Mono;
    return BiosConsole::Unknown;
}

std::string join_path(const char* directory, const char* name) {
    if (!directory || !*directory) return name;
    std::string out(directory);
    const char last = out.back();
    if (last != '/' && last != '\\') out.push_back('/');
    out += name;
    return out;
}

bool using_external_bios_boot() {
    return bios_boot && bios_kind == BiosKind::External;
}

BiosKind load_bios_image() {
    const char* system_dir = nullptr;
    if (environ_cb &&
        environ_cb(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system_dir) && system_dir) {
        static const char* names[] = {"ngpc_bios.bin", "ngpc-bios.bin", "bios.bin"};
        for (const char* name : names) {
            const std::string path = join_path(system_dir, name);
            std::ifstream file(path, std::ios::binary | std::ios::ate);
            if (!file || file.tellg() != 65536) continue;
            std::vector<uint8_t> data(65536);
            file.seekg(0);
            if (file.read(reinterpret_cast<char*>(data.data()), data.size()) &&
                ngpc_load_bios(machine, data.data(), data.size()) == 0) {
                bios_console = identify_bios(data.data(), data.size());
                log_message(RETRO_LOG_INFO, "NgpCraft: external BIOS loaded from %s (%s)\n",
                            path.c_str(),
                            bios_console == BiosConsole::Mono ? "monochrome NGP"
                            : bios_console == BiosConsole::Colour ? "colour NGPC"
                                                                  : "unrecognised image");
                return BiosKind::External;
            }
        }
    }
    if (ngpc_load_bios(machine, ngpcraft_firmware::bios_hle,
                       ngpcraft_firmware::bios_hle_size) == 0) {
        log_message(RETRO_LOG_INFO, "NgpCraft: using built-in clean-room HLE BIOS.\n");
        return BiosKind::BuiltInHle;
    }
    log_message(RETRO_LOG_ERROR, "NgpCraft: built-in HLE BIOS could not be loaded.\n");
    return BiosKind::None;
}

/* Cart-flash wait-states and block-copy costs. Not a display option and not a speed
 * hack: OFF is the un-timed machine, ON is the console. Safe to change at any time --
 * these are plain counters, not reset-time state. */
void apply_silicon_timing() {
    const bool on = !get_variable("ngpcraft_cart_timing", "disabled");
    if (on) {
        /* ⚖️ THE SILICON MODEL, ARMED -- three hardware measurements carry it, and the
         * determinism test that used to block it is green.
         *
         *  - CPU: ROM a_irq_calib_v8.ngp on a console (RASV=198, stable). Silicon
         *    261/218/249 batches, this model 260/218/250. An interrupt costs 111 cycles
         *    there, 113 here -- the PREVIOUS timing billed 59, half price, which moved
         *    every raster split.
         *  - Serial: the 21/08 AUTO campaign (every witness zero, ECHOED = sum of the
         *    round trips to the unit). Round trips within 0.3 %.
         *  - Cool Boarders' split misses, out of 600 frames: previous timing 122, this
         *    model 62.
         *
         * ⚡ WHAT UNBLOCKED IT. `libretro_smoke_external_bios_priority` reported
         * "non-deterministic state after replay" because the pipelined BIU's debt was
         * CLEARED on restore. That looks prudent and is not: the first run carries on
         * with its live value while the replay starts from zero, so the two diverge.
         * The debt now travels IN the aux block (it took `_pad2`, so the size is
         * unchanged and no existing save state is invalidated). */
        ngpc_set_timing_silicon(machine, 10u, 8u);
        
        
        
    } else {
        ngpc_set_cart_wait(machine, 0u);
        ngpc_set_cart_data_wait(machine, 0u);
        ngpc_set_ldir_cost(machine, 0u);
        ngpc_set_ldirw_cost(machine, 0u);
    }
}

/* ⚡ THE BIOS THAT BOOTS IS THE MACHINE. A cartridge asks 0x6F91 which console it is in,
 * and that byte is stamped by the BIOS -- so "which machine" and "which BIOS" are the
 * same question and must not get two answers. `auto` therefore believes the IMAGE, not
 * the file name: a mono dump dropped in the system directory boots a mono NGP. The
 * explicit setting still forces it, because it is the only way to run the mono machine
 * for someone who has no NGP dump at all (our HLE image is neither console's BIOS).
 *
 * ⛔ MUST RUN BEFORE ngpc_reset: reset_memory reads this flag to stamp 0x6F91/0x6F92. */
void apply_console_type() {
    const char* choice = get_variable_value("ngpcraft_console");
    const bool forced_mono = choice && std::strcmp(choice, "mono") == 0;
    const bool forced_colour = choice && std::strcmp(choice, "color") == 0;
    console_mono = forced_mono ||
                   (!forced_colour && bios_console == BiosConsole::Mono);
    ngpc_set_k1ge_console(machine, console_mono ? 1 : 0);
}

/* 🔫 GAMES THAT CHECK THE CONSOLE BOOTED FROM THE BIOS, BY FINGERPRINTING CHAR RAM.
 *
 * Metal Slug 2nd Mission carries its own 64-byte copy of a piece of the SNK BIOS's
 * boot-time char RAM and sweeps 0xA000..0xC000 for it. Miss, and it wipes the magic
 * "MET2" at 0x6A88; a routine then zeroes the key configuration every other frame, so
 * `and A,<mask>` is always `and A,0` -- the game runs, it LOOKS perfect, and shoot and
 * jump never fire again. A deliberately quiet punishment for what it takes to be a
 * pirate copy, and so the quietest possible bug report for us.
 *
 * The retail BIOS leaves that data at 0xA1C0 as a by-product of its own boot. Our
 * clean-room HLE image cannot: those bytes are SNK glyphs, and the check is exactly a
 * demand for SNK's own expression. So we ship none of it -- THE SIXTY-FOUR BYTES COME
 * OUT OF THE PLAYER'S OWN CARTRIDGE, which already contains them, and go into the
 * player's char RAM. This code stores facts (a title, an offset, an address), not data.
 *
 * ⚡ BEHAVIOUR-GATED, NOT BIOS-GATED: nothing happens if the fingerprint is already in
 * char RAM, which is the case with a real bios.bin, so it cannot regress that path.
 * Hand-off only -- under a console boot the BIOS is about to produce the data itself,
 * and writing here would pre-empt a boot that works. Ported from the desktop's
 * core/bios_fingerprint.py: it lived in that front end's Python layer, so a NATIVE front
 * end never had it, and with the built-in HLE BIOS -- the default for anyone without a
 * dump -- this game was shipping with its controls quietly disabled. */
constexpr uint32_t kCharRamBase = 0x00A000;
constexpr uint32_t kCharRamSize = 0x2000;

struct Fingerprint {
    const char* title;   // header name at 0x24, 16 bytes as stored (NUL-padded)
    uint32_t src;        // where the game keeps its own copy, as a ROM offset
    uint32_t length;     // how many bytes it compares
    uint32_t dest;       // where to put them -- where the retail BIOS has them
};

/* Only entries proven on hardware-faithful measurement belong here. The game's scan is a
 * SEARCH over the whole 8 KiB, so `dest` is free; 0xA1C0 is where the retail BIOS happens
 * to leave it, and it lands on tiles 0x1C-0x1F -- control codes, blank in our font. */
constexpr Fingerprint kFingerprints[] = {
    {"METALSLUG2ND\0\0\0", 0x08DCC4, 64, 0x00A1C0},
};

void apply_bios_fingerprint() {
    if (rom_image.size() < 0x40) return;
    for (const Fingerprint& fp : kFingerprints) {
        if (std::memcmp(rom_image.data() + 0x24, fp.title, 16) != 0) continue;
        if (rom_image.size() < fp.src + fp.length) continue;
        const uint8_t* want = rom_image.data() + fp.src;
        std::vector<uint8_t> char_ram(kCharRamSize);
        ngpc_read_mem(machine, kCharRamBase, char_ram.data(), kCharRamSize);
        if (contains_bytes(char_ram.data(), char_ram.size(), want, fp.length))
            return;                       // a real BIOS already satisfied the check
        ngpc_write_mem(machine, fp.dest, want, fp.length);
        log_message(RETRO_LOG_INFO,
                    "NgpCraft: restored this cartridge's own BIOS char-RAM fingerprint "
                    "at %06X (the game checks it within two frames).\n", fp.dest);
        return;
    }
}

/* 🔌 WHO THE CABLE GOES TO. A libretro front end has no text field to type an IP into --
 * core options are enumerations -- so the peer's address and the port come from a plain
 * file in the system directory, `ngpcraft_link.cfg`:
 *
 *     host = 192.168.1.20
 *     port = 7788
 *
 * The host side needs neither line (it listens on 7788 by default, the desktop's own
 * number, so a player hosting from either front end has nothing to tell the other).
 * Unreadable or absent, the defaults stand -- a missing config must not be a refusal. */
void load_link_config() {
    link_peer_address.clear();
    link_port = ngpcraft::kDefaultPort;
    const char* system_dir = nullptr;
    if (!environ_cb ||
        !environ_cb(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system_dir) || !system_dir)
        return;
    std::ifstream file(join_path(system_dir, "ngpcraft_link.cfg"));
    if (!file) return;
    std::string line;
    while (std::getline(file, line)) {
        const size_t equals = line.find('=');
        if (equals == std::string::npos || line.empty() || line[0] == '#') continue;
        auto trim = [](std::string s) {
            const char* space = " \t\r\n";
            const size_t a = s.find_first_not_of(space);
            if (a == std::string::npos) return std::string();
            return s.substr(a, s.find_last_not_of(space) - a + 1);
        };
        const std::string key = trim(line.substr(0, equals));
        const std::string value = trim(line.substr(equals + 1));
        if (key == "host") link_peer_address = value;
        else if (key == "port") {
            const long parsed = std::strtol(value.c_str(), nullptr, 10);
            if (parsed > 0 && parsed < 65536) link_port = static_cast<uint16_t>(parsed);
        }
    }
}

/* Arm, re-arm or drop the cable to match the option. Changing it mid-game is honest:
 * plugging a cable in is something a player does with the console running. */
void apply_link_option() {
    const char* choice = get_variable_value("ngpcraft_link");
    const bool host = choice && std::strcmp(choice, "host") == 0;
    const bool join = choice && std::strcmp(choice, "join") == 0;
    if (!host && !join) {
        if (cable.armed()) {
            log_message(RETRO_LOG_INFO, "NgpCraft: link cable unplugged.\n");
            ngpc_serial_set_enabled(machine, 0);
        }
        cable.shutdown();
        link_announced = false;
        return;
    }
    if (cable.armed()) return;              // already listening or connected
    load_link_config();
    const auto mode = host ? ngpcraft::CableLink::Mode::Host
                           : ngpcraft::CableLink::Mode::Join;
    if (!cable.start(mode, link_peer_address, link_port)) {
        log_message(RETRO_LOG_ERROR, "NgpCraft: link cable failed: %s\n",
                    cable.lost_reason().c_str());
        return;
    }
    link_announced = false;
    /* ⚡ THE HARDWARE PATH GOES LIVE NOW, not on connect. Disabled, the serial registers
     * stay inert and the cable reads as unplugged -- and a game that probes the cable
     * during its own boot would decide there is none before the peer ever arrives. */
    ngpc_serial_set_enabled(machine, 1);
    if (host)
        log_message(RETRO_LOG_INFO, "NgpCraft: hosting a link game on port %u -- the "
                    "other player joins this machine's address.\n", link_port);
    else
        log_message(RETRO_LOG_INFO, "NgpCraft: joining a link game at %s:%u.\n",
                    link_peer_address.empty() ? "(no host= in ngpcraft_link.cfg)"
                                              : link_peer_address.c_str(), link_port);
}

/* Every reset in this core goes through here, so the hand-off-only rule is stated once. */
void reset_machine() {
    const bool console_boot = using_external_bios_boot();
    ngpc_reset(machine, console_boot ? NGPC_RESET_BIOS_BOOT : NGPC_RESET_HANDOFF);
    if (!console_boot) apply_bios_fingerprint();
}

void configure_machine() {
    bios_boot = get_variable("ngpcraft_boot_mode", "bios");
    ngpc_set_language(machine, get_variable("ngpcraft_language", "japanese") ? 0u : 1u);
    apply_silicon_timing();
    apply_console_type();
}

uint32_t tail_address(unsigned chip, uint32_t capacity) {
    const uint32_t base = chip == 0 ? kCart0Base : kCart1Base;
    return base + capacity - kFlashTailSize;
}

bool valid_save_ram() {
    return save_ram.magic == kSaveMagic && save_ram.version == kSaveVersion &&
           save_ram.rom_id == rom_id;
}

bool valid_flash_capacity(unsigned chip, uint32_t capacity) {
    if (capacity < kFlashTailSize || capacity > 0x200000 ||
        (capacity & 0xFFFFu) != 0) return false;
    const size_t image_offset = chip == 0 ? 0 : 0x200000;
    if (rom_image.size() <= image_offset) return false;
    const size_t image_size = std::min<size_t>(rom_image.size() - image_offset, 0x200000);
    return capacity >= image_size;
}

void initialize_save_ram() {
    std::memset(&save_ram, 0xFF, sizeof(save_ram));
    save_ram.magic = kSaveMagic;
    save_ram.version = kSaveVersion;
    save_ram.rom_id = rom_id;
    for (unsigned chip = 0; chip < 2; ++chip) {
        const uint32_t capacity = ngpc_flash_capacity(machine, chip);
        save_ram.capacity[chip] = capacity;
        if (capacity >= kFlashTailSize)
            ngpc_read_mem(machine, tail_address(chip, capacity),
                          save_ram.flash_tail[chip], kFlashTailSize);
    }
    if (using_external_bios_boot())
        ngpc_read_mem(machine, 0x004000, save_ram.battery, kBatteryRamSize);
    ngpc_get_rtc(machine, &save_ram.rtc);
    save_loaded = true;
}

void restore_save_ram() {
    if (save_loaded) return;
    if (!valid_save_ram()) {
        log_message(RETRO_LOG_WARN,
                    "NgpCraft: incompatible or corrupt SAVE_RAM ignored; creating a clean save.\n");
        initialize_save_ram();
        return;
    }
    if (using_external_bios_boot())
        ngpc_set_battery_ram(machine, save_ram.battery, kBatteryRamSize);
    ngpc_set_rtc(machine, &save_ram.rtc);
    /* A real BIOS consults the coin-cell RAM during reset. RetroArch loads the
     * SRAM buffer after retro_load_game(), so repeat the power-on now that the
     * persisted battery contents are actually available. */
    if (using_external_bios_boot()) {
        ngpc_reset(machine, NGPC_RESET_BIOS_BOOT);
        power_pressed = false;
    }
    for (unsigned chip = 0; chip < 2; ++chip) {
        const uint32_t capacity = save_ram.capacity[chip];
        if (!valid_flash_capacity(chip, capacity)) continue;
        if (ngpc_flash_capacity(machine, chip) == 0) continue;
        ngpc_set_flash_size(machine, chip, capacity);
        ngpc_flash_restore(machine, tail_address(chip, capacity),
                           save_ram.flash_tail[chip], kFlashTailSize);
    }
    ngpc_flash_clear_dirty(machine);
    save_loaded = true;
}

void sync_save_ram() {
    if (!machine || !valid_save_ram()) return;
    for (unsigned chip = 0; chip < 2; ++chip) {
        const uint32_t capacity = ngpc_flash_capacity(machine, chip);
        save_ram.capacity[chip] = capacity;
        if (capacity >= kFlashTailSize)
            ngpc_read_mem(machine, tail_address(chip, capacity),
                          save_ram.flash_tail[chip], kFlashTailSize);
    }
    if (using_external_bios_boot())
        ngpc_read_mem(machine, 0x004000, save_ram.battery, kBatteryRamSize);
    ngpc_get_rtc(machine, &save_ram.rtc);
    ngpc_flash_clear_dirty(machine);
}

uint8_t read_pad() {
    if (!input_state_cb || controller_device == RETRO_DEVICE_NONE) return 0;
    uint16_t bits = 0;
    if (input_bitmasks) {
        bits = static_cast<uint16_t>(
            input_state_cb(0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_MASK));
    } else {
        static constexpr unsigned ids[] = {
            RETRO_DEVICE_ID_JOYPAD_UP, RETRO_DEVICE_ID_JOYPAD_DOWN,
            RETRO_DEVICE_ID_JOYPAD_LEFT, RETRO_DEVICE_ID_JOYPAD_RIGHT,
            RETRO_DEVICE_ID_JOYPAD_B, RETRO_DEVICE_ID_JOYPAD_A,
            RETRO_DEVICE_ID_JOYPAD_START
        };
        for (unsigned id : ids)
            if (input_state_cb(0, RETRO_DEVICE_JOYPAD, 0, id))
                bits |= uint16_t(1) << id;
    }
    uint8_t pad = 0;
    auto pressed = [bits](unsigned id) {
        return (bits & (uint16_t(1) << id)) != 0;
    };
    if (pressed(RETRO_DEVICE_ID_JOYPAD_UP)) pad |= 0x01;
    if (pressed(RETRO_DEVICE_ID_JOYPAD_DOWN)) pad |= 0x02;
    if (pressed(RETRO_DEVICE_ID_JOYPAD_LEFT)) pad |= 0x04;
    if (pressed(RETRO_DEVICE_ID_JOYPAD_RIGHT)) pad |= 0x08;
    if (pressed(RETRO_DEVICE_ID_JOYPAD_B)) pad |= 0x10; // NGPC A
    if (pressed(RETRO_DEVICE_ID_JOYPAD_A)) pad |= 0x20; // NGPC B
    if (pressed(RETRO_DEVICE_ID_JOYPAD_START)) pad |= 0x40;
    return pad;
}

void reset_with_persistent_state() {
    if (!machine) return;
    sync_save_ram();
    configure_machine();
    if (using_external_bios_boot())
        ngpc_set_battery_ram(machine, save_ram.battery, kBatteryRamSize);
    ngpc_set_rtc(machine, &save_ram.rtc);
    reset_machine();
    for (unsigned chip = 0; chip < 2; ++chip) {
        const uint32_t capacity = save_ram.capacity[chip];
        if (valid_flash_capacity(chip, capacity) &&
            ngpc_flash_capacity(machine, chip) != 0) {
            ngpc_set_flash_size(machine, chip, capacity);
            ngpc_flash_restore(machine, tail_address(chip, capacity),
                               save_ram.flash_tail[chip], kFlashTailSize);
        }
    }
    ngpc_flash_clear_dirty(machine);
    power_pressed = false;
    last_stop_status = 0xFF;
    last_stop_pc = 0xFFFFFFFFu;
}

/* ⚡ A LINKED FRAME IS NOT ONE CALL. Unlinked, the core runs the whole frame in one go --
 * slicing costs a few percent and buys nothing when there is no peer to hear the bytes.
 * Cabled, the frame is run in slices of `kCableSlice` instructions with a relay between
 * each, because a byte that waits for the end of the frame is a byte that arrives a frame
 * late, in one direction, always. This is `PlayPage._run_frame_relaying` on the desktop,
 * same slice, same backstop -- a cross-play session must not behave differently
 * depending on which front end each player is sitting at. */
void run_one_frame(ngpc_summary_t& summary) {
    if (!cable.connected()) {
        ngpc_run_frames(machine, 1, kRunawayInstructions, &summary);
        return;
    }
    /* Probe the core's own counter rather than tracking one here: a savestate load can
     * move the core without a host-side copy following. */
    ngpc_summary_t probe{};
    ngpc_run(machine, 0, nullptr, 0, &probe);
    const uint32_t start = probe.frame_count;
    uint32_t executed = 0;
    for (unsigned slice = 0; slice < ngpcraft::kMaxSlices; ++slice) {
        ngpc_run(machine, ngpcraft::kCableSlice, nullptr, 0, &summary);
        executed += summary.executed;
        cable.pump(machine);
        if (summary.stop_status != NGPC_COUNT_REACHED && summary.stop_status != NGPC_OK)
            break;
        if (summary.executed == 0) break;            // the core refused to advance
        if (summary.frame_count != start) {          // the frame is done
            summary.executed = executed;
            return;
        }
    }
    // Never leave a frame half-run: whatever happened, finish it the plain way.
    ngpc_run_frames(machine, 1, kRunawayInstructions, &summary);
    cable.pump(machine);
    summary.executed = executed + summary.executed;
}

size_t state_size() {
    return sizeof(StateHeader) + sizeof(ngpc_cpu_t) + sizeof(ngpc_aux_state_t) +
           sizeof(ngpc_link_state_t) + sizeof(ngpc_rtc_t) + kWorkImageSize;
}

} // namespace

extern "C" {

RETRO_API unsigned retro_api_version(void) { return RETRO_API_VERSION; }

RETRO_API void retro_set_environment(retro_environment_t cb) {
    environ_cb = cb;
    static const retro_variable vars[] = {
        {"ngpcraft_boot_mode", "Boot mode; handoff|bios"},
        {"ngpcraft_language", "Console language; english|japanese"},
        /* auto = whichever console the loaded BIOS is the BIOS of; see
         * apply_console_type(). Takes effect on reset, like the boot mode. */
        {"ngpcraft_console", "Console type; auto|color|mono"},
        /* Hardware cart-flash timing. Default ON: OFF is the un-timed machine, where a
         * self-timed game runs about twice its real speed. See apply_silicon_timing(). */
        {"ngpcraft_cart_timing", "Cartridge wait-states (hardware speed); enabled|disabled"},
        /* The link cable, to another RetroArch or to the NgpCraft desktop emulator --
         * same wire, so the two cross-play. The peer's address lives in
         * <system>/ngpcraft_link.cfg; see load_link_config(). */
        {"ngpcraft_link", "Link cable (2 players, LAN/VPN); off|host|join"},
        {nullptr, nullptr}
    };
    if (environ_cb) {
        environ_cb(RETRO_ENVIRONMENT_SET_VARIABLES, (void*)vars);
        bool no_game = false;
        environ_cb(RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME, &no_game);
    }
}

RETRO_API void retro_set_video_refresh(retro_video_refresh_t cb) { video_cb = cb; }
RETRO_API void retro_set_audio_sample(retro_audio_sample_t cb) { audio_cb = cb; }
RETRO_API void retro_set_audio_sample_batch(retro_audio_sample_batch_t cb) { audio_batch_cb = cb; }
RETRO_API void retro_set_input_poll(retro_input_poll_t cb) { input_poll_cb = cb; }
RETRO_API void retro_set_input_state(retro_input_state_t cb) { input_state_cb = cb; }

RETRO_API void retro_init(void) {
    retro_log_callback logging{};
    if (environ_cb && environ_cb(RETRO_ENVIRONMENT_GET_LOG_INTERFACE, &logging))
        log_cb = logging.log;
    input_bitmasks = environ_cb &&
        environ_cb(RETRO_ENVIRONMENT_GET_INPUT_BITMASKS, nullptr);
}

RETRO_API void retro_deinit(void) {
    if (machine) ngpc_destroy(machine);
    machine = nullptr;
    rom_image.clear();
    save_loaded = false;
    bios_kind = BiosKind::None;
    bios_console = BiosConsole::Unknown;
    console_mono = false;
    cable.shutdown();
    link_announced = false;
    input_bitmasks = false;
    log_cb = nullptr;
}

RETRO_API void retro_get_system_info(struct retro_system_info* info) {
    std::memset(info, 0, sizeof(*info));
    info->library_name = "NgpCraft";
    info->library_version = "0.4-libretro";
    info->valid_extensions = "ngp|ngc|npc|bin";
    info->need_fullpath = false;
    info->block_extract = false;
}

RETRO_API void retro_get_system_av_info(struct retro_system_av_info* info) {
    std::memset(info, 0, sizeof(*info));
    info->geometry.base_width = kWidth;
    info->geometry.base_height = kHeight;
    info->geometry.max_width = kWidth;
    info->geometry.max_height = kHeight;
    info->geometry.aspect_ratio = 20.0f / 19.0f;
    info->timing.fps = kFps;
    info->timing.sample_rate = kSampleRate;
}

RETRO_API void retro_set_controller_port_device(unsigned port, unsigned device) {
    if (port == 0) controller_device = device;
}

RETRO_API void retro_reset(void) { reset_with_persistent_state(); }

RETRO_API bool retro_load_game(const struct retro_game_info* game) {
    if (!game || !game->data || game->size < 0x30 || game->size > kMaximumRomSize)
        return false;
    if (machine) ngpc_destroy(machine);
    machine = ngpc_create();
    if (!machine) return false;

    rom_image.assign(static_cast<const uint8_t*>(game->data),
                     static_cast<const uint8_t*>(game->data) + game->size);
    rom_id = fnv1a(rom_image.data(), rom_image.size());
    if (ngpc_load_rom(machine, rom_image.data(), rom_image.size()) != 0) {
        ngpc_destroy(machine);
        machine = nullptr;
        return false;
    }

    configure_machine();
    bios_console = BiosConsole::Unknown;
    bios_kind = load_bios_image();
    if (bios_boot && bios_kind != BiosKind::External)
        log_message(RETRO_LOG_WARN,
                    "NgpCraft: console boot needs an external BIOS; using the built-in "
                    "HLE BIOS in hand-off mode.\n");
    /* configure_machine() ran before the BIOS was on disk-read, so `auto` had nothing to
     * believe yet. Ask again now that the image has identified itself -- and before the
     * reset that stamps the machine-type bytes. */
    apply_console_type();
    log_message(RETRO_LOG_INFO, "NgpCraft: console = %s, cart wait-states %s.\n",
                console_mono ? "Neo Geo Pocket (monochrome)" : "Neo Geo Pocket Color",
                get_variable("ngpcraft_cart_timing", "disabled") ? "OFF (untimed)"
                                                                 : "ON (silicon)");

    reset_machine();
    initialize_save_ram();
    save_loaded = false; // RetroArch may replace the SRAM buffer after this call.
    power_pressed = false;
    last_stop_status = 0xFF;
    last_stop_pc = 0xFFFFFFFFu;

    enum retro_pixel_format pixel_format = RETRO_PIXEL_FORMAT_XRGB8888;
    if (!environ_cb || !environ_cb(RETRO_ENVIRONMENT_SET_PIXEL_FORMAT, &pixel_format)) {
        log_message(RETRO_LOG_ERROR, "NgpCraft: frontend does not support XRGB8888.\n");
        retro_unload_game();
        return false;
    }

    static const retro_input_descriptor inputs[] = {
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_UP, "D-Pad Up"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_DOWN, "D-Pad Down"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_LEFT, "D-Pad Left"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_RIGHT, "D-Pad Right"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_B, "A"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_A, "B"},
        {0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_START, "Option"},
        {0, 0, 0, 0, nullptr}
    };
    environ_cb(RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS, (void*)inputs);
    /* ⛔ THE CABLE IS ARMED HERE, NOT ONLY ON AN OPTION CHANGE. It used to be applied
     * solely from the GET_VARIABLE_UPDATE branch in retro_run, so a player who set
     * "Link cable = host" and THEN launched a game got no cable at all: the option was
     * already at its final value, so it never "changed". Nothing reported it -- the core
     * simply played on alone, which is indistinguishable from a peer who never showed up. */
    apply_link_option();
    log_message(RETRO_LOG_INFO, "NgpCraft: loaded %zu-byte cartridge (ABI %u).\n",
                rom_image.size(), ngpc_abi_version());
    return true;
}

RETRO_API bool retro_load_game_special(unsigned, const struct retro_game_info*, size_t) {
    return false;
}

RETRO_API void retro_unload_game(void) {
    if (machine) {
        sync_save_ram();
        ngpc_destroy(machine);
    }
    machine = nullptr;
    rom_image.clear();
    save_loaded = false;
    bios_kind = BiosKind::None;
    bios_console = BiosConsole::Unknown;
    console_mono = false;
    cable.shutdown();
    link_announced = false;
}

RETRO_API unsigned retro_get_region(void) { return RETRO_REGION_NTSC; }

RETRO_API void* retro_get_memory_data(unsigned id) {
    return id == RETRO_MEMORY_SAVE_RAM && machine ? &save_ram : nullptr;
}

RETRO_API size_t retro_get_memory_size(unsigned id) {
    return id == RETRO_MEMORY_SAVE_RAM && machine ? sizeof(save_ram) : 0;
}

RETRO_API size_t retro_serialize_size(void) { return machine ? state_size() : 0; }

RETRO_API bool retro_serialize(void* data, size_t size) {
    if (!machine || !data || size < state_size()) return false;
    uint8_t* out = static_cast<uint8_t*>(data);
    const StateHeader header{kStateMagic, kStateVersion, ngpc_abi_version(), rom_id};
    ngpc_cpu_t cpu{};
    ngpc_aux_state_t aux{};
    ngpc_link_state_t link{};
    ngpc_rtc_t rtc{};
    ngpc_get_cpu(machine, &cpu);
    ngpc_get_aux_state(machine, &aux);
    ngpc_get_link_state(machine, &link);
    ngpc_get_rtc(machine, &rtc);
    // A clamped cable is an INEXACT snapshot, and netplay built on it would desync
    // without ever saying why. Refuse to hand one out.
    if (link.overflow) return false;
    std::memcpy(out, &header, sizeof(header)); out += sizeof(header);
    std::memcpy(out, &cpu, sizeof(cpu)); out += sizeof(cpu);
    std::memcpy(out, &aux, sizeof(aux)); out += sizeof(aux);
    std::memcpy(out, &link, sizeof(link)); out += sizeof(link);
    std::memcpy(out, &rtc, sizeof(rtc)); out += sizeof(rtc);
    return ngpc_read_mem(machine, 0, out, kWorkImageSize) == 0;
}

RETRO_API bool retro_unserialize(const void* data, size_t size) {
    if (!machine || !data || size < state_size()) return false;
    const uint8_t* in = static_cast<const uint8_t*>(data);
    StateHeader header{};
    ngpc_cpu_t cpu{};
    ngpc_aux_state_t aux{};
    ngpc_link_state_t link{};
    ngpc_rtc_t rtc{};
    std::memcpy(&header, in, sizeof(header)); in += sizeof(header);
    if (header.magic != kStateMagic || header.version != kStateVersion ||
        header.abi != ngpc_abi_version() || header.rom_id != rom_id) return false;
    std::memcpy(&cpu, in, sizeof(cpu)); in += sizeof(cpu);
    std::memcpy(&aux, in, sizeof(aux)); in += sizeof(aux);
    std::memcpy(&link, in, sizeof(link)); in += sizeof(link);
    std::memcpy(&rtc, in, sizeof(rtc)); in += sizeof(rtc);
    if (ngpc_write_mem(machine, 0, in, kWorkImageSize) != 0) return false;
    ngpc_set_cpu(machine, &cpu);
    if (ngpc_set_aux_state(machine, &aux) != 0) return false;
    // ...after the image, like the aux block: SC0MOD/BR0CR live in the image and decide
    // how fast a byte shifts.
    if (ngpc_set_link_state(machine, &link) != 0) return false;
    ngpc_set_rtc(machine, &rtc);
    return true;
}

RETRO_API void retro_cheat_reset(void) {}
RETRO_API void retro_cheat_set(unsigned, bool, const char*) {}

RETRO_API void retro_run(void) {
    if (!machine) return;
    restore_save_ram();
    /* Options the player changed mid-game. ONLY the timing is re-applied live: it is a
     * pair of counters and takes effect on the next instruction. The console type and the
     * boot mode are stamped into memory by the reset, so re-applying them now would put
     * the machine and the bytes a running game already read out of agreement -- they wait
     * for retro_reset, which is where configure_machine() picks them up. */
    bool options_changed = false;
    if (environ_cb &&
        environ_cb(RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE, &options_changed) &&
        options_changed) {
        apply_silicon_timing();
        apply_link_option();
    }
    /* The cable, once per frame: a host with nobody there yet and a joiner whose peer is
     * not up both keep playing normally -- which is what a console with the cable in the
     * socket and nothing at the other end does. */
    if (cable.poll_connection()) {
        log_message(RETRO_LOG_INFO, "NgpCraft: link cable connected.\n");
        link_announced = true;
    } else if (cable.state() == ngpcraft::CableLink::State::Lost && !cable.lost_reason().empty()) {
        if (link_announced || !cable.lost_reason().empty()) {
            log_message(RETRO_LOG_WARN, "NgpCraft: link cable lost -- %s\n",
                        cable.lost_reason().c_str());
            ngpc_serial_set_enabled(machine, 0);
            cable.shutdown();
            link_announced = false;
        }
    }
    if (input_poll_cb) input_poll_cb();
    const uint8_t pad = read_pad();
    ngpc_write_mem(machine, 0x00B0, &pad, 1);

    ngpc_summary_t summary{};
    run_one_frame(summary);
    if (using_external_bios_boot() && !power_pressed &&
        summary.stop_status == NGPC_HALTED) {
        ngpc_raise_irq(machine, 8);
        power_pressed = true;
        run_one_frame(summary);
    }
    if (summary.stop_status != NGPC_COUNT_REACHED && summary.stop_status != NGPC_OK &&
        summary.stop_status != NGPC_HALTED &&
        (summary.stop_status != last_stop_status || summary.stop_pc != last_stop_pc)) {
        log_message(RETRO_LOG_ERROR, "NgpCraft: core stopped (status %u, PC %06X, opcode %02X).\n",
                    summary.stop_status, summary.stop_pc, summary.stop_opcode);
        last_stop_status = summary.stop_status;
        last_stop_pc = summary.stop_pc;
    }

    const uint32_t pixel_count = static_cast<uint32_t>(native_video.size());
    if (ngpc_get_framebuffer(machine, native_video.data(), pixel_count) == pixel_count) {
        for (size_t i = 0; i < native_video.size(); ++i) {
            const uint16_t p = native_video[i];
            const uint32_t r = (p & 0x000F) * 17u;
            const uint32_t g = ((p >> 4) & 0x000F) * 17u;
            const uint32_t b = ((p >> 8) & 0x000F) * 17u;
            video[i] = (r << 16) | (g << 8) | b;
        }
    }
    if (video_cb) video_cb(video.data(), kWidth, kHeight, kWidth * sizeof(uint32_t));

    const uint32_t audio_capacity = static_cast<uint32_t>(audio.size() / 2);
    const uint32_t frames = ngpc_get_audio(machine, audio.data(), audio_capacity);
    if (audio_batch_cb) audio_batch_cb(audio.data(), frames);
    else if (audio_cb) {
        for (uint32_t i = 0; i < frames; ++i) audio_cb(audio[i * 2], audio[i * 2 + 1]);
    }

    if (ngpc_flash_dirty(machine)) {
        sync_save_ram();
    } else {
        /* RetroArch may flush SRAM before retro_unload_game(). Keep the
         * coin-cell domain current continuously, not only when flash changed. */
        if (using_external_bios_boot())
            ngpc_read_mem(machine, 0x004000, save_ram.battery, kBatteryRamSize);
        ngpc_get_rtc(machine, &save_ram.rtc);
    }
}

} // extern "C"
