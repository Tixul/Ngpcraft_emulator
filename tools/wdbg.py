"""A minimal Win32 debugger: run a command, and when it dies, say WHERE.

`__fastfail` (STATUS_STACK_BUFFER_OVERRUN, 0xC0000409) deliberately bypasses every
exception handler in the process -- faulthandler cannot see it, and neither can a
vectored handler. Only an attached DEBUGGER gets the event. So we attach.

We track LOAD_DLL_DEBUG_EVENT to learn each module's base address, and on the
fatal exception we map the faulting RIP back to `module+offset`. That alone says
whose code raised it, which is the question.
"""
from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import sys

k32 = C.WinDLL("kernel32", use_last_error=True)
psapi = C.WinDLL("psapi", use_last_error=True)

DEBUG_ONLY_THIS_PROCESS = 0x00000002
INFINITE = 0xFFFFFFFF
DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001

EXCEPTION_DEBUG_EVENT = 1
CREATE_THREAD_DEBUG_EVENT = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_THREAD_DEBUG_EVENT = 4
EXIT_PROCESS_DEBUG_EVENT = 5
LOAD_DLL_DEBUG_EVENT = 6
UNLOAD_DLL_DEBUG_EVENT = 7
OUTPUT_DEBUG_STRING_EVENT = 8


class STARTUPINFOW(C.Structure):
    _fields_ = [
        ("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR), ("dwX", W.DWORD), ("dwY", W.DWORD),
        ("dwXSize", W.DWORD), ("dwYSize", W.DWORD), ("dwXCountChars", W.DWORD),
        ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
        ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
        ("lpReserved2", C.POINTER(C.c_byte)), ("hStdInput", W.HANDLE),
        ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE),
    ]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class EXCEPTION_RECORD(C.Structure):
    pass


EXCEPTION_RECORD._fields_ = [
    ("ExceptionCode", W.DWORD), ("ExceptionFlags", W.DWORD),
    ("ExceptionRecord", C.POINTER(EXCEPTION_RECORD)),
    ("ExceptionAddress", C.c_void_p), ("NumberParameters", W.DWORD),
    ("ExceptionInformation", C.c_ulonglong * 15),
]


class EXCEPTION_DEBUG_INFO(C.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", W.DWORD)]


class LOAD_DLL_DEBUG_INFO(C.Structure):
    _fields_ = [("hFile", W.HANDLE), ("lpBaseOfDll", C.c_void_p),
                ("dwDebugInfoFileOffset", W.DWORD), ("nDebugInfoSize", W.DWORD),
                ("lpImageName", C.c_void_p), ("fUnicode", W.WORD)]


class CREATE_PROCESS_DEBUG_INFO(C.Structure):
    _fields_ = [("hFile", W.HANDLE), ("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("lpBaseOfImage", C.c_void_p), ("dwDebugInfoFileOffset", W.DWORD),
                ("nDebugInfoSize", W.DWORD), ("lpThreadLocalBase", C.c_void_p),
                ("lpStartAddress", C.c_void_p), ("lpImageName", C.c_void_p),
                ("fUnicode", W.WORD)]


class EXIT_PROCESS_DEBUG_INFO(C.Structure):
    _fields_ = [("dwExitCode", W.DWORD)]


class OUTPUT_DEBUG_STRING_INFO(C.Structure):
    _fields_ = [("lpDebugStringData", C.c_void_p), ("fUnicode", W.WORD),
                ("nDebugStringLength", W.WORD)]


class DEBUG_EVENT_UNION(C.Union):
    _fields_ = [
        ("Exception", EXCEPTION_DEBUG_INFO),
        ("CreateProcessInfo", CREATE_PROCESS_DEBUG_INFO),
        ("LoadDll", LOAD_DLL_DEBUG_INFO),
        ("ExitProcess", EXIT_PROCESS_DEBUG_INFO),
        ("_pad", C.c_byte * 176),
    ]


class DEBUG_EVENT(C.Structure):
    _fields_ = [("dwDebugEventCode", W.DWORD), ("dwProcessId", W.DWORD),
                ("dwThreadId", W.DWORD), ("u", DEBUG_EVENT_UNION)]


def module_name(h_process: int, base: int) -> str:
    buf = C.create_unicode_buffer(1024)
    if psapi.GetMappedFileNameW(W.HANDLE(h_process), C.c_void_p(base), buf, 1024):
        return buf.value.split("\\")[-1]
    return "?"


def main() -> int:
    cmdline = " ".join(sys.argv[1:])
    si = STARTUPINFOW()
    si.cb = C.sizeof(si)
    pi = PROCESS_INFORMATION()
    if not k32.CreateProcessW(
        None, C.create_unicode_buffer(cmdline), None, None, True,
        DEBUG_ONLY_THIS_PROCESS, None, None, C.byref(si), C.byref(pi)
    ):
        print("CreateProcessW failed:", C.get_last_error())
        return 1

    modules: dict[int, str] = {}          # base -> name
    ev = DEBUG_EVENT()
    exit_code = 0

    while True:
        if not k32.WaitForDebugEvent(C.byref(ev), INFINITE):
            break
        status = DBG_CONTINUE
        code = ev.dwDebugEventCode

        if code == CREATE_PROCESS_DEBUG_EVENT:
            base = ev.u.CreateProcessInfo.lpBaseOfImage or 0
            modules[int(base)] = module_name(pi.hProcess, int(base))

        elif code == LOAD_DLL_DEBUG_EVENT:
            base = ev.u.LoadDll.lpBaseOfDll or 0
            modules[int(base)] = module_name(pi.hProcess, int(base))

        elif code == EXCEPTION_DEBUG_EVENT:
            rec = ev.u.Exception.ExceptionRecord
            first = ev.u.Exception.dwFirstChance
            ecode = rec.ExceptionCode & 0xFFFFFFFF
            addr = int(rec.ExceptionAddress or 0)
            # 0x80000003 = the initial breakpoint the loader always raises.
            if ecode not in (0x80000003, 0x4000001F, 0x406D1388):
                owner_base = max((b for b in modules if b <= addr), default=0)
                owner = modules.get(owner_base, "?")
                print(
                    f"\n*** EXCEPTION 0x{ecode:08X}"
                    f"{' (first chance)' if first else ' (SECOND CHANCE -- FATAL)'}"
                    f"\n    address : 0x{addr:016X}"
                    f"\n    module  : {owner} + 0x{addr - owner_base:X}",
                    flush=True,
                )
                for i in range(min(rec.NumberParameters, 4)):
                    print(f"    param{i}  : 0x{rec.ExceptionInformation[i]:X}", flush=True)
                status = DBG_EXCEPTION_NOT_HANDLED
            else:
                status = DBG_CONTINUE

        elif code == OUTPUT_DEBUG_STRING_EVENT:
            # Qt's message handler writes here on Windows. A qFatal() message --
            # the reason it is about to abort -- comes out on THIS channel, not on
            # stderr, which is why the suite died without ever saying why.
            info = C.cast(C.byref(ev.u), C.POINTER(OUTPUT_DEBUG_STRING_INFO)).contents
            n = info.nDebugStringLength
            buf = C.create_string_buffer(n + 2)
            read = C.c_size_t(0)
            k32.ReadProcessMemory(
                W.HANDLE(pi.hProcess), C.c_void_p(info.lpDebugStringData),
                buf, n, C.byref(read),
            )
            raw = buf.raw[: read.value]
            text = (raw.decode("utf-16-le", "replace") if info.fUnicode
                    else raw.decode("mbcs", "replace")).rstrip("\x00\r\n")
            if text.strip():
                print(f"[debug-output] {text}", flush=True)

        elif code == EXIT_PROCESS_DEBUG_EVENT:
            exit_code = ev.u.ExitProcess.dwExitCode
            print(f"\n*** process exited with 0x{exit_code & 0xFFFFFFFF:08X}", flush=True)
            k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)
            break

        k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)

    return 0


if __name__ == "__main__":
    sys.exit(main())
