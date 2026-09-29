/* 🔌 THE LINK CABLE, OVER TCP -- the crossplay half of this port.
 *
 * ⚡ THIS IS NOT A NEW PROTOCOL. It is the desktop shell's `core.link.TcpLink`, byte for
 * byte, so a RetroArch player and a NgpCraft-desktop player are two consoles on one
 * cable. THE WIRE CARRIES THE CABLE'S OWN BYTES AND NOTHING ELSE -- no header, no
 * framing, no handshake, no version field. That is not laziness: a link game writes a
 * byte and blocks on the peer's answer, so anything wrapped around those bytes would be
 * latency taken straight out of the game's speed. It also means the two ends need no
 * agreement whatsoever beyond the port number, which is what makes crossplay possible
 * between two binaries that share nothing but this core.
 *
 * TCP because the cable never drops or reorders a byte, which is exactly TCP's promise.
 *
 * ⛔ WHAT A RELAYED CABLE COSTS, measured on the desktop (Fatal Fury, the game's own
 * logic counter, per emulated frame):
 *
 *     round trip     0 ms   33 ms   67 ms   134 ms
 *     game speed     1.00   0.80    0.57    0.36
 *
 * Every millisecond of ping comes out of the GAME, not out of the picture -- that is
 * the "the fight is in slow motion but the audio is perfect" report, and it is inherent
 * to relaying a cable rather than a bug to fix here. LAN and VPN are where this mode
 * lives; the mirror mode is the answer for a real internet link.
 *
 * ⚠️ NO CTS CROSS-WIRING, DELIBERATELY. Two consoles in ONE process cross-wire the
 * hardware handshake (each console's CTS0 pin is the peer's RTS line); over TCP the
 * desktop does not, because the peer's RTS is a round trip away and a flow-control line
 * sampled a round trip late is worse than one that is never asserted. Matching that
 * exactly matters more than improving on it: a difference here is a game that hangs
 * against one of the two front ends and not the other.
 *
 * ⚠️ AND RX IS PUSHED UNCONDITIONALLY, not gated on our own RTS. The core's serial_tick
 * is the flow-control gate -- it only presents a byte to the CPU once RTS is low -- so
 * delivering early just queues it, exactly like a real cable. Gating here could strand
 * a handshake byte and read to the player as "no cable".
 */
#ifndef NGPCRAFT_CABLE_LINK_HPP
#define NGPCRAFT_CABLE_LINK_HPP

#include "ngpc_core.h"

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <winsock2.h>
#  include <ws2tcpip.h>
   typedef SOCKET ngpcraft_socket_t;
#  define NGPCRAFT_INVALID_SOCKET INVALID_SOCKET
#else
#  include <arpa/inet.h>
#  include <errno.h>
#  include <fcntl.h>
#  include <netinet/in.h>
#  include <netinet/tcp.h>
#  include <sys/socket.h>
#  include <unistd.h>
   typedef int ngpcraft_socket_t;
#  define NGPCRAFT_INVALID_SOCKET (-1)
#endif

namespace ngpcraft {

/* One TLCS-900 SC0 byte at 19200 bps is ~3200 CPU cycles and a frame is ~102 000, so
 * ~32 bytes cross per frame at most. Drain well past that so a burst is never left
 * stranded in the FIFO for a frame. Same number as core/link.py's _DRAIN_CHUNK. */
constexpr uint32_t kDrainChunk = 256;

/* ⚡ HOW OFTEN THE CABLE IS RELAYED WITHIN A FRAME, in instructions.
 *
 * ⛔ A CORRECTNESS FIGURE, NOT A TUNING KNOB. Relaying once per frame puts a whole
 * frame of latency on every answer, and Card Fighters' Clash's VS handshake dies of
 * exactly that: its packet reader gives up when the next byte is not already in the
 * BIOS ring. The desktop measured The Last Blade needing it no coarser than 400 (2000
 * already fails). This is `core.link.CABLE_SLICE`, and the two front ends must use the
 * SAME number or a cross-play session behaves differently depending on who hosts. */
constexpr uint32_t kCableSlice = 400;

/* A frame is a few thousand instructions, so this is a runaway backstop with a wide
 * margin, not a target: whatever happens, the caller finishes the frame the plain way. */
constexpr unsigned kMaxSlices = 256;

/* The desktop's own default (ngpc_lobby.DEFAULT_PORT). Sharing it means a player can
 * host on either front end and the other side needs to be told nothing. */
constexpr uint16_t kDefaultPort = 7788;

inline void socket_startup() {
#ifdef _WIN32
    static bool started = false;
    if (!started) {
        WSADATA wsa{};
        started = (WSAStartup(MAKEWORD(2, 2), &wsa) == 0);
    }
#endif
}

inline void close_socket(ngpcraft_socket_t& s) {
    if (s == NGPCRAFT_INVALID_SOCKET) return;
#ifdef _WIN32
    closesocket(s);
#else
    ::close(s);
#endif
    s = NGPCRAFT_INVALID_SOCKET;
}

inline void set_non_blocking(ngpcraft_socket_t s) {
#ifdef _WIN32
    u_long mode = 1;
    ioctlsocket(s, FIONBIO, &mode);
#else
    const int flags = fcntl(s, F_GETFL, 0);
    fcntl(s, F_SETFL, (flags < 0 ? 0 : flags) | O_NONBLOCK);
#endif
}

inline void set_no_delay(ngpcraft_socket_t s) {
    const int on = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&on), sizeof(on));
}

/* "try again later", as opposed to "the peer is gone". Telling the two apart is the
 * whole difference between a link that survives a full kernel buffer and one that
 * silently stops carrying bytes. */
inline bool would_block() {
#ifdef _WIN32
    const int e = WSAGetLastError();
    return e == WSAEWOULDBLOCK || e == WSAEINPROGRESS || e == WSAEALREADY;
#else
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR || errno == EINPROGRESS;
#endif
}

inline int last_socket_error() {
#ifdef _WIN32
    return WSAGetLastError();
#else
    return errno;
#endif
}

class CableLink {
public:
    enum class Mode { Off, Host, Join };
    enum class State { Idle, Waiting, Connected, Lost };

    ~CableLink() { shutdown(); }

    State state() const { return state_; }
    bool connected() const { return state_ == State::Connected; }
    bool armed() const { return state_ == State::Waiting || state_ == State::Connected; }
    const std::string& lost_reason() const { return lost_; }
    uint64_t bytes_out() const { return bytes_out_; }
    uint64_t bytes_in() const { return bytes_in_; }

    /* Start listening (host) or start connecting (join). Neither blocks: a host with
     * nobody there yet and a joiner whose peer is not up are both State::Waiting, and
     * the game runs normally meanwhile -- exactly like a console with the cable in the
     * socket and nothing at the other end. */
    bool start(Mode mode, const std::string& address, uint16_t port) {
        shutdown();
        if (mode == Mode::Off) return false;
        socket_startup();
        mode_ = mode;
        lost_.clear();
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port);
        if (mode == Mode::Host) {
            addr.sin_addr.s_addr = htonl(INADDR_ANY);
            listener_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
            if (listener_ == NGPCRAFT_INVALID_SOCKET) return fail("socket() failed");
            const int reuse = 1;
            setsockopt(listener_, SOL_SOCKET, SO_REUSEADDR,
                       reinterpret_cast<const char*>(&reuse), sizeof(reuse));
            if (bind(listener_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0)
                return fail("could not bind the link port");
            if (listen(listener_, 1) != 0) return fail("could not listen on the link port");
            set_non_blocking(listener_);
        } else {
            if (inet_pton(AF_INET, address.c_str(), &addr.sin_addr) != 1)
                return fail("the peer address is not a valid IPv4 address");
            peer_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
            if (peer_ == NGPCRAFT_INVALID_SOCKET) return fail("socket() failed");
            set_non_blocking(peer_);
            remote_ = addr;
            if (connect(peer_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 &&
                !would_block())
                return fail("could not reach the host");
        }
        state_ = State::Waiting;
        return true;
    }

    /* `machine` may be null when the console is already gone; the declaration dies with
     * it either way. Passing it lets the GAME see the cable being unplugged. */
    void shutdown(ngpc_t* machine = nullptr) {
        drop_peer(machine);
        peer_declared_ = false;
        close_socket(listener_);
        close_socket(peer_);
        out_.clear();
        state_ = State::Idle;
        mode_ = Mode::Off;
        bytes_out_ = bytes_in_ = 0;
    }

    /* Called once per frame while waiting. Returns true the moment the cable comes up. */
    bool poll_connection() {
        if (state_ != State::Waiting) return false;
        if (mode_ == Mode::Host) {
            const ngpcraft_socket_t s = accept(listener_, nullptr, nullptr);
            if (s == NGPCRAFT_INVALID_SOCKET) return false;
            close_socket(listener_);
            peer_ = s;
            set_non_blocking(peer_);
            set_no_delay(peer_);
            state_ = State::Connected;
            return true;
        }
        /* A non-blocking connect finishes silently; the portable way to notice is to
         * ask for the socket's error state, which is 0 once it is through. Retrying
         * connect() instead reports EISCONN on some stacks and success on others. */
        fd_set write_set;
        FD_ZERO(&write_set);
        FD_SET(peer_, &write_set);
        timeval zero{0, 0};
#ifdef _WIN32
        fd_set error_set;
        FD_ZERO(&error_set);
        FD_SET(peer_, &error_set);
        const int ready = select(0, nullptr, &write_set, &error_set, &zero);
        if (ready > 0 && FD_ISSET(peer_, &error_set)) { fail("the host refused the link"); return false; }
#else
        const int ready = select(peer_ + 1, nullptr, &write_set, nullptr, &zero);
#endif
        if (ready <= 0 || !FD_ISSET(peer_, &write_set)) return false;
        int err = 0;
        socklen_t len = sizeof(err);
        if (getsockopt(peer_, SOL_SOCKET, SO_ERROR,
                       reinterpret_cast<char*>(&err), &len) != 0 || err != 0) {
            fail("the host refused the link");
            return false;
        }
        set_no_delay(peer_);
        state_ = State::Connected;
        return true;
    }

    /* One relay, to be called after every slice of emulation -- NOT once per frame.
     * Mirrors core.link.TcpLink.pump step for step. */
    void pump(ngpc_t* machine) {
        if (state_ != State::Connected || !machine) return;

        /* ⛔ DECLARE THE PEER, OR THE GAME NEVER SEES THE CABLE.
         *
         * 0xB1 bit2 is the cable-DETECT line games gate their versus mode on, and the
         * core only clears it once something has SPOKEN FOR THE PEER
         * (`serial_cts_seen`) -- it follows the peer's RTS, measured on silicon
         * 2026-08-19. A relay that only moves bytes says nothing, so every console on
         * this path read "no cable" for ever: Card Fighters' Clash stuck on "EITHER
         * PLAYER MUST PUSH A", Gals' Fighters / Puyo Pop reporting no second player.
         *
         * ⚡ AND IT IS A CROSSPLAY REQUIREMENT, not a local nicety. The desktop
         * declares it (core.link.declare_peer, 2026-09-02). A desktop console paired
         * with this one would see a peer while this one saw none -- one side offers
         * the versus menu, the other refuses it, and the cable looks broken from
         * exactly one end. The two ends must answer the same question the same way.
         *
         * The socket is connected here, and a cable session is only ever started from
         * a console already running a cartridge, so "a peer is attached" and "the peer
         * is a console running a game" are the same statement on this path. */
        if (!peer_declared_) {
            ngpc_serial_set_cts(machine, 0);   /* peer present and ready */
            peer_declared_ = true;
        }

        /* 1) local TX -> the socket. Drained into one buffer so a burst crosses whole. */
        uint8_t chunk[kDrainChunk];
        for (;;) {
            const uint32_t n = ngpc_serial_read_tx(machine, chunk, kDrainChunk);
            if (n == 0) break;
            out_.insert(out_.end(), chunk, chunk + n);
            if (n < kDrainChunk) break;
        }
        if (!out_.empty()) {
            /* ⛔ NOT a blocking "send it all". A non-blocking send reports what it TOOK;
             * dropping the remainder would lose cable bytes mid-stream, and a cable that
             * loses bytes is a game that hangs waiting for a packet nobody sent. Keep the
             * rest and offer it again next slice -- the frame is relayed hundreds of
             * times, so a full kernel buffer costs a fraction of a frame, not a byte. */
            const int sent = static_cast<int>(
                send(peer_, reinterpret_cast<const char*>(out_.data()),
                     static_cast<int>(out_.size()), 0));
            if (sent > 0) {
                bytes_out_ += static_cast<uint64_t>(sent);
                out_.erase(out_.begin(), out_.begin() + sent);
            } else if (sent < 0 && !would_block()) {
                lose("the peer went away while sending");
                drop_peer(machine);
                return;
            }
        }

        /* 2) the socket -> the local RX FIFO. */
        uint8_t buffer[4096];
        for (;;) {
            const int got = static_cast<int>(
                recv(peer_, reinterpret_cast<char*>(buffer), sizeof(buffer), 0));
            if (got > 0) {
                ngpc_serial_write_rx(machine, buffer, static_cast<uint32_t>(got));
                bytes_in_ += static_cast<uint64_t>(got);
                if (got < static_cast<int>(sizeof(buffer))) break;
                continue;
            }
            if (got == 0) {
                /* A clean FIN. Leaving the loop instead would make a peer that quit
                 * tidily indistinguishable from one with nothing to say, and the game
                 * would sit waiting for a console that has gone. */
                lose("the peer closed the connection");
                drop_peer(machine);
                return;
            }
            if (!would_block()) {
                lose("the peer went away");
                drop_peer(machine);
            }
            break;
        }
    }

private:
    /* The cable is gone: take the detect line down so the GAME sees the cut. That is how
     * Match of the Millennium raises its own link error on hardware, and until the line
     * followed the peer it could only ever be handled host-side. */
    void drop_peer(ngpc_t* machine) {
        if (peer_declared_ && machine) {
            ngpc_serial_set_cts(machine, 1);
            peer_declared_ = false;
        }
    }

    bool fail(const char* why) {
        lost_ = why;
        close_socket(listener_);
        close_socket(peer_);
        state_ = State::Lost;
        return false;
    }

    void lose(const char* why) {
        if (lost_.empty()) {
            lost_ = why;
            lost_ += " (error ";
            lost_ += std::to_string(last_socket_error());
            lost_ += ")";
        }
        close_socket(peer_);
        close_socket(listener_);
        state_ = State::Lost;
    }

    ngpcraft_socket_t listener_ = NGPCRAFT_INVALID_SOCKET;
    ngpcraft_socket_t peer_ = NGPCRAFT_INVALID_SOCKET;
    sockaddr_in remote_{};
    Mode mode_ = Mode::Off;
    State state_ = State::Idle;
    bool peer_declared_ = false;     /* see pump(): 0xB1 bit2 needs someone to speak */
    std::string lost_;
    std::vector<uint8_t> out_;      /* written but not yet accepted by the kernel */
    uint64_t bytes_out_ = 0;
    uint64_t bytes_in_ = 0;
};

} // namespace ngpcraft

#endif // NGPCRAFT_CABLE_LINK_HPP
