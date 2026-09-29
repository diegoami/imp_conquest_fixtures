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
  Imperial Conquest 2 fast rollingsave.exe   fast, and the game saves itself
  Imperial Conquest 2 watch rollingsave.exe  watch, and the game saves itself:
                                 before each human turn starts it writes
                                 AUTOnnnn.SAV beside the exe and appends
                                 "nnnn AUTOnnnn.SAV OK" (or FAIL, BUSY, LONG)
                                 to AUTOSAVE.LOG there.
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

# Autosave: the seat-start routine calls StartTurn at HOOK_SITE once the AI
# seats and the weekly tick are done; the call is sent through a cave that
# saves first. The cave goes after the delay routine, growing .patch to 0x600.
HOOK_SITE = 0x45217A                    # call StartTurn
START_TURN = 0x45AC5C
SAVE_GAME = 0x4484D0                    # writes the save to the name in FILE_NAME
FILE_NAME = 0x45E7A8                    # char[100]
BATTLE_FLAG = 0x4A0B7C
SEASON, WEEK, YEAR_BC = 0x4A032E, 0x4A0330, 0x4A0332
HANDLE_ANY_EXCEPTION = 0x402D6C         # RTL: try/except handler, _DoneExcept
DONE_EXCEPT = 0x403088
CLOSE_HANDLE = 0x40489C
CREATE_FILE = 0x4048AC
GET_MODULE_FILE_NAME = 0x4048FC
SET_FILE_POINTER = 0x40499C
WRITE_FILE = 0x4049BC
CAVE_VA, CAVE_RAW, PATCH_SIZE = EXTRA_VA + 0x200, EXTRA_RAW + 0x200, 0x600


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
    ("jmp", va), ("abs", name) for a label's address, ("j", opcode, name)
    for short jumps, ("jn", opcode, name) for the near form of a short jcc."""
    labels, out = {}, bytearray()
    for _ in range(2):
        out = bytearray()
        for it in items:
            if isinstance(it, bytes):
                out += it
            elif it[0] == "label":
                labels[it[1]] = len(out)
            elif it[0] in ("call", "jmp"):
                out += (b"\xE8" if it[0] == "call" else b"\xE9") + rel32(origin + len(out), it[1])
            elif it[0] == "abs":
                out += struct.pack("<I", origin + labels.get(it[1], 0))
            elif it[0] == "jn":
                rel = labels.get(it[2], len(out)) - (len(out) + 6)
                out += bytes([0x0F, it[1] + 0x10]) + struct.pack("<i", rel)
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


def ebp(op, disp, imm=b""):
    return op + struct.pack("<i", disp) + imm


def autosave(b):
    # Before StartTurn, save to AUTOnnnn.SAV beside the exe and append
    # "nnnn AUTOnnnn.SAV STAT" to AUTOSAVE.LOG there, STAT being OK, FAIL (the
    # save raised), BUSY (battle pending, not saved) or LONG (path > 99 chars,
    # not saved). nnnn = (300 - year BC) * 24 + season * 6 + (week - 1) / 2.
    # The player's own file name is put back after the save.
    PATH, SAVED, LINE, WRITTEN, NAME = -0x120, -0x184, -0x19C, -0x1A0, -0x1A8
    OK, FAIL, BUSY, LONG = (struct.pack("<4s", s) for s in (b"OK  ", b"FAIL", b"BUSY", b"LONG"))
    JB, JE, JA, JNE, JMP = 0x72, 0x74, 0x77, 0x75, 0xEB

    def digit(i):                                 # eax /= 10, digit i = remainder
        return b"\x33\xD2\xF7\xF3\x80\xC2\x30" + ebp(b"\x88\x95", LINE + i)

    code = assemble(CAVE_VA, [
        b"\x60\x55\x8B\xEC",                      # pushad; push ebp; mov ebp, esp
        b"\x81\xEC" + struct.pack("<I", -NAME),     # sub  esp, locals
        b"\x0F\xBF\x05" + struct.pack("<I", YEAR_BC),
        b"\xB9\x2C\x01\x00\x00\x2B\xC8",          # mov ecx, 300; sub ecx, eax
        b"\x6B\xC9\x18",                          # imul ecx, ecx, 24
        b"\x0F\xBF\x05" + struct.pack("<I", SEASON),
        b"\x6B\xC0\x06\x03\xC8",                  # imul eax, eax, 6; add ecx, eax
        b"\x0F\xBF\x05" + struct.pack("<I", WEEK),
        b"\x48\xD1\xF8\x03\xC8",                  # dec eax; sar eax, 1; add ecx, eax
        b"\x8B\xC1\xBB\x0A\x00\x00\x00",          # mov eax, ecx; mov ebx, 10
        digit(3), digit(2), digit(1), digit(0),     # LINE = "nnnn AUTOnnnn.SAV ????\r\n"
        ebp(b"\xC6\x85", LINE + 4, b" "),
        ebp(b"\xC7\x85", LINE + 5, b"AUTO"),
        ebp(b"\x8B\x85", LINE), ebp(b"\x89\x85", LINE + 9),
        ebp(b"\xC7\x85", LINE + 13, b".SAV"),
        ebp(b"\xC6\x85", LINE + 17, b" "),
        ebp(b"\x66\xC7\x85", LINE + 22, b"\r\n"),
        b"\x68\x04\x01\x00\x00",                  # GetModuleFileNameA(0, PATH, 260)
        ebp(b"\x8D\x85", PATH), b"\x50\x6A\x00",
        ("call", GET_MODULE_FILE_NAME),
        ebp(b"\x8D\xBD", PATH), b"\x03\xF8",        # edi = PATH + length
        ("label", "back"),                          # back up to the last backslash
        b"\x4F", ebp(b"\x8D\x85", PATH), b"\x3B\xF8", ("j", JB, "dir"),
        b"\x80\x3F\x5C", ("j", JNE, "back"),
        ("label", "dir"),
        b"\x47", ebp(b"\x89\xBD", NAME),            # inc edi; NAME = edi
        ebp(b"\x8D\xB5", LINE + 5),                 # PATH = dir + "AUTOnnnn.SAV"
        b"\xB9\x0C\x00\x00\x00\xF3\xA4\xC6\x07\x00",
        ebp(b"\xC7\x85", LINE + 18, BUSY),
        b"\x80\x3D" + struct.pack("<I", BATTLE_FLAG) + b"\x00", ("jn", JNE, "log"),
        ebp(b"\xC7\x85", LINE + 18, LONG),
        ebp(b"\x8D\x85", PATH), b"\x8B\xCF\x2B\xC8",  # ecx = strlen(PATH)
        b"\x83\xF9\x63", ("jn", JA, "log"),
        b"\x41\x51",                               # inc ecx; push ecx
        b"\xBE" + struct.pack("<I", FILE_NAME),     # SAVED = FILE_NAME
        ebp(b"\x8D\xBD", SAVED), b"\xB9\x64\x00\x00\x00\xF3\xA4",
        b"\x59", ebp(b"\x8D\xB5", PATH),            # FILE_NAME = PATH
        b"\xBF" + struct.pack("<I", FILE_NAME), b"\xF3\xA4",
        ebp(b"\xC7\x85", LINE + 18, FAIL),
        b"\x33\xC0\x55\x68", ("abs", "except"),     # try
        b"\x64\xFF\x30\x64\x89\x20",
        ("call", SAVE_GAME),
        ebp(b"\xC7\x85", LINE + 18, OK),
        b"\x33\xC0\x5A\x59\x59\x64\x89\x10",      # end try
        ("j", JMP, "restore"),
        ("label", "except"),
        ("jmp", HANDLE_ANY_EXCEPTION),
        ("call", DONE_EXCEPT),
        ("label", "restore"),                       # FILE_NAME = SAVED
        ebp(b"\x8D\xB5", SAVED), b"\xBF" + struct.pack("<I", FILE_NAME),
        b"\xB9\x64\x00\x00\x00\xF3\xA4",
        ("label", "log"),                           # PATH = dir + "AUTOSAVE.LOG"
        ebp(b"\x8B\xBD", NAME),
        b"\xC7\x07AUTO\xC7\x47\x04SAVE\xC7\x47\x08.LOG\xC6\x47\x0C\x00",
        b"\x6A\x00\x68\x80\x00\x00\x00\x6A\x04\x6A\x00\x6A\x01\x68\x00\x00\x00\x40",
        ebp(b"\x8D\x85", PATH), b"\x50",            # CreateFileA(PATH, GENERIC_WRITE,
        ("call", CREATE_FILE),                      #   share read, 0, OPEN_ALWAYS, normal, 0)
        b"\x83\xF8\xFF", ("j", JE, "done"),
        b"\x8B\xD8\x6A\x02\x6A\x00\x6A\x00\x53",  # SetFilePointer(h, 0, 0, FILE_END)
        ("call", SET_FILE_POINTER),
        b"\x6A\x00", ebp(b"\x8D\x85", WRITTEN), b"\x50\x6A\x18",
        ebp(b"\x8D\x85", LINE), b"\x50\x53",         # WriteFile(h, LINE, 24, &WRITTEN, 0)
        ("call", WRITE_FILE),
        b"\x53", ("call", CLOSE_HANDLE),
        ("label", "done"),
        b"\x8B\xE5\x5D\x61",                      # mov esp, ebp; pop ebp; popad
        ("jmp", START_TURN),                        # eax (Self) as the caller left it
    ])
    assert len(code) <= PATCH_SIZE - (CAVE_RAW - EXTRA_RAW)

    hdr = struct.unpack_from("<8sIIII", b, EXTRA_HEADER)
    if hdr == (bytes(8), 0, EXTRA_VA - BASE, 0, EXTRA_RAW) and len(b) == EXTRA_RAW:
        b += b"\xCC" * (CAVE_RAW - EXTRA_RAW)       # no delay routine (fast)
        struct.pack_into("<H", b, NUM_SECTIONS_FIELD, 9)
        struct.pack_into("<I", b, SIZE_OF_IMAGE_FIELD, EXTRA_VA - BASE + 0x1000)
    elif hdr != (b".patch\0\0", EXTRA_SIZE, EXTRA_VA - BASE, EXTRA_SIZE, EXTRA_RAW) \
            or len(b) != CAVE_RAW:
        raise SystemExit("spare section header not as expected")
    struct.pack_into("<8sIIIIIIHHI", b, EXTRA_HEADER, b".patch", PATCH_SIZE, EXTRA_VA - BASE,
                     PATCH_SIZE, EXTRA_RAW, 0, 0, 0, 0, 0x60000020)
    b += code.ljust(PATCH_SIZE - (CAVE_RAW - EXTRA_RAW), b"\xCC")
    patch(b, HOOK_SITE, b"\xE8" + rel32(HOOK_SITE, START_TURN), b"\xE8" + rel32(HOOK_SITE, CAVE_VA))


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
    build("Imperial Conquest 2 fast rollingsave.exe", async_sound, no_delay, autosave)
    build("Imperial Conquest 2 watch rollingsave.exe", async_sound, pumping_delay, autosave)
