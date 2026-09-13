#!/usr/bin/env python3
"""Build patched copies of Imperial Conquest 2.exe that don't freeze during battles.

    py patch_exe.py

The original plays every sound synchronously and animates battles with a
busy-wait delay (n x 100 ms per shot/attack) that never handles window
messages. On modern Windows the window is then ghosted as "Not Responding"
for the whole battle. Two variants are written next to the original:

  Imperial Conquest 2 fast.exe   sounds async, battle delay removed (instant battles)
  Imperial Conquest 2 watch.exe  sounds async, battle delay kept, but while it
                                 waits it handles window messages so the battle
                                 stays visible. Keyboard and mouse input is
                                 discarded during the wait, so clicks can't
                                 interfere with a battle in progress.
"""

import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / "Imperial Conquest 2.exe"

BASE = 0x400000
CODE_VA, CODE_RAW = 0x401000, 0x400
CODE_VSIZE_FIELD = 0x1F8 + 8            # section header #0 VirtualSize

GET_TICK_COUNT = 0x404914               # import thunks (jmp [IAT])
DISPATCH_MESSAGE = 0x404C4C
PEEK_MESSAGE = 0x404E6C
DELAY = 0x448FFC                        # Delay(n in eax): wait n x 100 ms
PLAY_SOUND_FLAGS = 0x45C013             # push 0 (fdwSound) before PlaySoundA

# The linker left an unused 9th section header (VA 0x164000, raw at end of
# file); it becomes a small code section for the new delay routine.
NUM_SECTIONS_FIELD = 0x106
SIZE_OF_IMAGE_FIELD = 0x118 + 56
EXTRA_HEADER = 0x1F8 + 8 * 40
EXTRA_VA, EXTRA_RAW, EXTRA_SIZE = 0x564000, 0x11CC00, 0x200


def off(va):
    return va - CODE_VA + CODE_RAW


def rel32(src, target):
    return struct.pack("<i", target - (src + 5))


def patch(b, va, expect, new):
    o = off(va)
    if b[o:o + len(expect)] != expect:
        raise SystemExit("unexpected bytes at 0x%x: %s" % (va, b[o:o + len(expect)].hex()))
    b[o:o + len(new)] = new


def async_sound(b):
    # PlaySoundA(name, 0, SND_SYNC) -> SND_ASYNC | SND_NODEFAULT
    patch(b, PLAY_SOUND_FLAGS, bytes.fromhex("6A006A008D44240850E8"), b"\x6A\x03")


def no_delay(b):
    patch(b, DELAY, bytes.fromhex("53568BF0E8"), b"\xC3")


def assemble(origin, items):
    """Tiny two-pass assembler: bytes, ("label", name), ("call", va),
    ("j", opcode, name) for short jumps."""
    labels, out = {}, bytearray()
    for _ in range(2):
        out = bytearray()
        for it in items:
            if isinstance(it, bytes):
                out += it
            elif it[0] == "label":
                labels[it[1]] = len(out)
            elif it[0] == "call":
                out += b"\xE8" + rel32(origin + len(out), it[1])
            else:
                rel = labels.get(it[2], len(out)) - (len(out) + 2)
                out += bytes([it[1]]) + struct.pack("<b", rel)
    return bytes(out)


def cmp_eax(v):
    return b"\x3D" + struct.pack("<I", v)


def pumping_delay(b):
    # Delay(n): wait n x 100 ms like the original, but keep taking messages off
    # the queue so Windows never considers the game hung. Keyboard/mouse input
    # (and posted WM_SYSCOMMAND) is discarded; everything else is dispatched.
    JB, JBE, JE, JLE, JMP, JZ = 0x72, 0x76, 0x74, 0x7E, 0xEB, 0x74
    code = assemble(EXTRA_VA, [
        b"\x53\x56",                              # push ebx; push esi
        b"\x83\xEC\x1C",                          # sub  esp, 28          ; MSG
        b"\x6B\xF0\x64",                          # imul esi, eax, 100    ; wait ms
        ("call", GET_TICK_COUNT),
        b"\x8B\xD8",                              # mov  ebx, eax         ; start
        ("label", "loop"),
        b"\x6A\x01\x6A\x00\x6A\x00\x6A\x00",      # push PM_REMOVE, 0, 0, NULL
        b"\x8D\x44\x24\x10\x50",                  # lea  eax, [esp+16]; push eax
        ("call", PEEK_MESSAGE),
        b"\x85\xC0", ("j", JZ, "time"),           # no message
        b"\x8B\x44\x24\x04",                      # mov  eax, [esp+4]     ; msg.message
        cmp_eax(0x0A0), ("j", JB, "dispatch"),
        cmp_eax(0x0AF), ("j", JBE, "time"),       # non-client mouse
        cmp_eax(0x100), ("j", JB, "dispatch"),
        cmp_eax(0x10F), ("j", JBE, "time"),       # keyboard
        cmp_eax(0x112), ("j", JE, "time"),        # WM_SYSCOMMAND
        cmp_eax(0x200), ("j", JB, "dispatch"),
        cmp_eax(0x20F), ("j", JBE, "time"),       # mouse
        ("label", "dispatch"),
        b"\x54",                                  # push esp              ; &MSG
        ("call", DISPATCH_MESSAGE),
        ("label", "time"),
        ("call", GET_TICK_COUNT),
        b"\x2B\xC3\x3B\xC6",                      # sub eax, ebx; cmp eax, esi
        ("j", JLE, "loop"),
        b"\x83\xC4\x1C\x5E\x5B\xC3",              # add esp, 28; pop esi; pop ebx; ret
    ])
    assert len(code) <= EXTRA_SIZE

    hdr = bytes(b[EXTRA_HEADER:EXTRA_HEADER + 40])
    if struct.unpack_from("<8sIIII", hdr) != (bytes(8), 0, EXTRA_VA - BASE, 0, EXTRA_RAW) \
            or len(b) != EXTRA_RAW:
        raise SystemExit("spare section header not as expected")
    struct.pack_into("<8sIIIIIIHHI", b, EXTRA_HEADER, b".patch", EXTRA_SIZE, EXTRA_VA - BASE,
                     EXTRA_SIZE, EXTRA_RAW, 0, 0, 0, 0, 0x60000020)  # code, execute, read
    struct.pack_into("<H", b, NUM_SECTIONS_FIELD, 9)
    struct.pack_into("<I", b, SIZE_OF_IMAGE_FIELD, EXTRA_VA - BASE + 0x1000)
    b += code.ljust(EXTRA_SIZE, b"\xCC")
    patch(b, DELAY, bytes.fromhex("53568BF0E8"), b"\xE9" + rel32(DELAY, EXTRA_VA))


def build(name, *patches):
    b = bytearray(ORIGINAL.read_bytes())
    if struct.unpack_from("<I", b, CODE_VSIZE_FIELD)[0] != 0x5B3D4:
        raise SystemExit("%s is not the expected original exe" % ORIGINAL.name)
    for p in patches:
        p(b)
    out = HERE / name
    if out.exists() and out.read_bytes() == b:
        print("up to date", out.name)
        return
    try:
        out.write_bytes(b)
    except PermissionError:
        raise SystemExit("can't write %s: close the game first" % out.name)
    print("wrote", out.name)


if __name__ == "__main__":
    build("Imperial Conquest 2 fast.exe", async_sound, no_delay)
    build("Imperial Conquest 2 watch.exe", async_sound, pumping_delay)
