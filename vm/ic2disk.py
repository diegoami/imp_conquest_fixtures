#!/usr/bin/env python3
"""Move Imperial Conquest 2 into a Windows 98 VM, and its save games back out.

Windows 98 can't use VirtualBox shared folders, so this builds a small virtual
hard disk instead: a fixed-size VHD (64 MB, MBR + one FAT16 partition) holding
the game. Attach it to the VM as a second IDE disk and Windows 98 sees it as D:.

    py vm/ic2disk.py build          # create vm/ic2disk.vhd (game + saves/)
    py vm/ic2disk.py list  [disk]   # show what is on the disk
    py vm/ic2disk.py pull  [disk]   # copy *.sav from the disk into saves/

Only read the disk while the VM is powered off, and shut Windows 98 down
properly first (Start > Shut Down) so it has flushed its write cache.
Standard library only; works on any OS with Python 3.6+.
"""

import argparse
import datetime
import hashlib
import os
import random
import struct
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DISK = REPO / "vm" / "ic2disk.vhd"
GAME_DIR = "IC2"
GAME_FILES = [
    "Imperial Conquest 2.exe",
    "Imperial Conquest 2.dat",
    "Imperial Conquest 2.hlp",
    "Imperial Conquest 2.cnt",
    "Read_Me.txt",
]

SECTOR = 512
# 130 x 16 x 63 geometry: the classic translation Windows 98 expects, and the
# disk size is an exact multiple of it so CHS and LBA agree everywhere.
CYLS, HEADS, SPT = 130, 16, 63
TOTAL_SECTORS = CYLS * HEADS * SPT          # 131040 sectors = ~64 MB
PART_START = SPT                            # partition starts at C0/H1/S1
PART_SECTORS = TOTAL_SECTORS - PART_START
SEC_PER_CLUS = 4                            # 2 KB clusters, as FORMAT would pick
RESERVED = 1
NUM_FATS = 2
ROOT_ENTRIES = 512
ROOT_SECTORS = ROOT_ENTRIES * 32 // SECTOR
CLUSTER_BYTES = SEC_PER_CLUS * SECTOR

ATTR_RO, ATTR_VOLUME, ATTR_DIR, ATTR_ARCHIVE, ATTR_LFN = 0x01, 0x08, 0x10, 0x20, 0x0F
SHORT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&")


# ---------------------------------------------------------------- FAT helpers

def dos_datetime(ts):
    t = time.localtime(ts)
    year = min(max(t.tm_year, 1980), 2107)
    date = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tim = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return date, tim


def from_dos_datetime(date, tim):
    try:
        return datetime.datetime(
            1980 + (date >> 9), (date >> 5) & 0x0F, date & 0x1F,
            tim >> 11, (tim >> 5) & 0x3F, (tim & 0x1F) * 2)
    except ValueError:
        return None


def lfn_checksum(name11):
    s = 0
    for b in name11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def is_valid_83(name):
    base, dot, ext = name.partition(".")
    return (1 <= len(base) <= 8 and len(ext) <= 3 and "." not in ext
            and not (dot and not ext)
            and all(c in SHORT_CHARS for c in base + ext))


def make_short_name(name, used):
    """Return (11-byte short name, needs_lfn) the way Windows 98 derives it."""
    upper = name.upper()
    if is_valid_83(upper):
        base, _, ext = upper.partition(".")
        packed = (base.ljust(8) + ext.ljust(3)).encode("ascii")
        if packed not in used:
            used.add(packed)
            return packed, name != upper

    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""

    def clean(s):
        s = s.upper().replace(" ", "").replace(".", "")
        return "".join(c if c in SHORT_CHARS else "_" for c in s)

    base, ext = clean(stem) or "_", clean(ext)[:3]
    for n in range(1, 1000000):
        tail = "~%d" % n
        packed = ((base[:8 - len(tail)] + tail).ljust(8) + ext.ljust(3)).encode("ascii")
        if packed not in used:
            used.add(packed)
            return packed, True
    raise RuntimeError("out of short names for %r" % name)


def lfn_entries(name, checksum):
    chars = [name[i:i + 1].encode("utf-16-le") for i in range(len(name))]
    if len(chars) % 13:
        chars.append(b"\0\0")
        chars += [b"\xff\xff"] * (-len(chars) % 13)
    count = len(chars) // 13
    out = []
    for i in range(count):
        part = chars[i * 13:(i + 1) * 13]
        seq = (i + 1) | (0x40 if i == count - 1 else 0)
        out.append(bytes([seq]) + b"".join(part[0:5]) + bytes([ATTR_LFN, 0, checksum])
                   + b"".join(part[5:11]) + b"\0\0" + b"".join(part[11:13]))
    return out[::-1]  # stored last-part-first, right before the short entry


def dir_entry(name11, attr, cluster, size, mtime):
    date, tim = dos_datetime(mtime)
    return struct.pack("<11sBBBHHHHHHHI", name11, attr, 0, 0, tim, date, date,
                       0, tim, date, cluster, size)


# ---------------------------------------------------------------- build

class Node:
    def __init__(self, name, src=None, children=None, mtime=None):
        self.name = name
        self.src = src                  # Path for files, None for directories
        self.children = children       # list of Node for directories
        self.mtime = mtime if mtime is not None else time.time()
        self.cluster = 0


def fat_size_sectors():
    fatsz = 1
    while True:
        clusters = (PART_SECTORS - RESERVED - NUM_FATS * fatsz - ROOT_SECTORS) // SEC_PER_CLUS
        need = -(-(clusters + 2) * 2 // SECTOR)
        if need <= fatsz:
            assert 4085 <= clusters < 65525, clusters
            return fatsz, clusters
        fatsz = need


class Fat16Builder:
    def __init__(self):
        self.fatsz, self.clusters = fat_size_sectors()
        self.fat = [0xFFF8, 0xFFFF] + [0] * self.clusters
        self.next = 2
        self.part = bytearray(PART_SECTORS * SECTOR)
        self.data_start = (RESERVED + NUM_FATS * self.fatsz + ROOT_SECTORS) * SECTOR

    def alloc(self, nbytes):
        count = max(1, -(-nbytes // CLUSTER_BYTES))
        first = self.next
        if first + count > self.clusters + 2:
            raise SystemExit("disk full: make the image bigger")
        for c in range(first, first + count - 1):
            self.fat[c] = c + 1
        self.fat[first + count - 1] = 0xFFFF
        self.next += count
        return first

    def write_cluster_data(self, cluster, data):
        off = self.data_start + (cluster - 2) * CLUSTER_BYTES
        self.part[off:off + len(data)] = data

    def entries_for(self, children, used):
        """Short names + entry count for a directory's children."""
        plan = []
        for child in children:
            short, needs_lfn = make_short_name(child.name, used)
            plan.append((child, short, needs_lfn))
        return plan

    def encode_entries(self, plan):
        out = bytearray()
        for child, short, needs_lfn in plan:
            if needs_lfn:
                for e in lfn_entries(child.name, lfn_checksum(short)):
                    out += e
            if child.src is None:
                out += dir_entry(short, ATTR_DIR, child.cluster, 0, child.mtime)
            else:
                size = child.src.stat().st_size
                out += dir_entry(short, ATTR_ARCHIVE, child.cluster if size else 0, size, child.mtime)
        return out

    def place(self, children, parent_cluster):
        for child in children:
            if child.src is not None:
                data = child.src.read_bytes()
                if data:
                    child.cluster = self.alloc(len(data))
                    self.write_cluster_data(child.cluster, data)
            else:
                self.build_dir(child, parent_cluster)

    def build_dir(self, node, parent_cluster):
        plan = self.entries_for(node.children, set())
        size = 64 + sum(32 * (1 + (len(lfn_entries(c.name, 0)) if lfn else 0))
                        for c, _, lfn in plan)
        node.cluster = self.alloc(size)
        self.place(node.children, node.cluster)
        body = (dir_entry(b".          ", ATTR_DIR, node.cluster, 0, node.mtime)
                + dir_entry(b"..         ", ATTR_DIR, parent_cluster, 0, node.mtime)
                + self.encode_entries(plan))
        self.write_cluster_data(node.cluster, body)

    def build(self, root_children, label):
        plan = self.entries_for(root_children, set())
        self.place(root_children, 0)
        root = dir_entry(label, ATTR_VOLUME, 0, 0, time.time()) + self.encode_entries(plan)
        if len(root) > ROOT_SECTORS * SECTOR:
            raise SystemExit("too many entries in the root directory")
        root_off = (RESERVED + NUM_FATS * self.fatsz) * SECTOR
        self.part[root_off:root_off + len(root)] = root

        fat_bytes = struct.pack("<%dH" % len(self.fat), *self.fat)
        for i in range(NUM_FATS):
            off = (RESERVED + i * self.fatsz) * SECTOR
            self.part[off:off + len(fat_bytes)] = fat_bytes

        boot = bytearray(SECTOR)
        boot[0:3] = b"\xEB\x3C\x90"
        boot[3:11] = b"MSWIN4.1"
        boot[11:36] = struct.pack("<HBHBHHBHHHII", SECTOR, SEC_PER_CLUS, RESERVED, NUM_FATS,
                                  ROOT_ENTRIES, 0, 0xF8, self.fatsz, SPT, HEADS,
                                  PART_START, PART_SECTORS)
        boot[36:62] = struct.pack("<BBBI11s8s", 0x80, 0, 0x29, random.getrandbits(32),
                                  label, b"FAT16   ")
        boot[62:67] = b"\xCD\x18\xF4\xEB\xFD"   # not bootable: int 18h, then halt
        boot[510:512] = b"\x55\xAA"
        self.part[0:SECTOR] = boot
        return bytes(self.part)


def chs(lba):
    c, rem = divmod(lba, HEADS * SPT)
    h, s = divmod(rem, SPT)
    return bytes([h, ((s + 1) & 0x3F) | ((c >> 2) & 0xC0), c & 0xFF])


def mbr():
    m = bytearray(SECTOR)
    m[0:5] = b"\xCD\x18\xF4\xEB\xFD"
    m[440:444] = struct.pack("<I", random.getrandbits(32))
    m[446:462] = (b"\x00" + chs(PART_START) + b"\x06" + chs(TOTAL_SECTORS - 1)
                  + struct.pack("<II", PART_START, PART_SECTORS))
    m[510:512] = b"\x55\xAA"
    return bytes(m)


def vhd_footer():
    size = TOTAL_SECTORS * SECTOR
    stamp = int(time.time() - datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
    f = bytearray(512)
    f[0:64] = struct.pack(">8sIIQI4sI4sQQHBBI", b"conectix", 2, 0x00010000,
                          0xFFFFFFFFFFFFFFFF, stamp, b"ic2 ", 0x00010000, b"Wi2k",
                          size, size, CYLS, HEADS, SPT, 2)  # disk type 2 = fixed
    f[68:84] = uuid.uuid4().bytes
    f[64:68] = struct.pack(">I", ~sum(f) & 0xFFFFFFFF)
    return bytes(f)


def file_node(path, name=None):
    return Node(name or path.name, src=path, mtime=path.stat().st_mtime)


def cmd_build(args):
    out = Path(args.out)
    if out.exists() and not args.force:
        sys.exit("%s already exists and may hold saves you haven't pulled.\n"
                 "Run 'pull' first, then 'build --force' to replace it." % out)

    wavs = sorted((REPO / "WAVS").glob("*.WAV"), key=lambda p: p.name.upper())
    saves = sorted((REPO / "saves").glob("*.sav"))
    game = Node(GAME_DIR, children=
                [file_node(REPO / f) for f in GAME_FILES]
                + [Node("WAVS", children=[file_node(p) for p in wavs])]
                + [Node("saves", children=[file_node(p) for p in saves])])

    part = Fat16Builder().build([game], b"IC2        ")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with open(str(tmp), "wb") as f:
        f.write(mbr())
        f.write(b"\0" * ((PART_START - 1) * SECTOR))
        f.write(part)
        f.write(vhd_footer())
    os.replace(str(tmp), str(out))
    print("Wrote %s (%d MB): D:\\%s with %d game files, %d sounds, %d saves."
          % (out, TOTAL_SECTORS * SECTOR // 2**20, GAME_DIR, len(GAME_FILES), len(wavs), len(saves)))


# ---------------------------------------------------------------- read

class FatVolume:
    """Read-only FAT12/16/32 reader for a raw image or fixed VHD."""

    def __init__(self, path):
        self.f = open(str(path), "rb")
        size = os.path.getsize(str(path))
        self.f.seek(max(size - 512, 0))
        tail = self.f.read(512)
        head = self._read(0, 512)
        if tail[:8] == b"conectix" or head[:8] == b"conectix":
            footer = tail if tail[:8] == b"conectix" else head
            if struct.unpack(">I", footer[60:64])[0] != 2:
                sys.exit("%s is a dynamic VHD; only fixed-size VHDs (as built by this script) "
                         "can be read. Open it with 7-Zip instead." % path)

        self.base = 0
        if head[510:512] == b"\x55\xAA" and not self._looks_like_boot(head):
            for i in range(4):
                e = head[446 + 16 * i:462 + 16 * i]
                if e[4] in (0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E):
                    self.base = struct.unpack("<I", e[8:12])[0] * SECTOR
                    break
            else:
                sys.exit("no FAT partition found on %s" % path)

        bs = self._read(self.base, 512)
        (self.bps, self.spc, reserved, nfats, root_entries, tot16, _, fatsz16,
         _, _, _, tot32) = struct.unpack("<HBHBHHBHHHII", bs[11:36])
        fatsz = fatsz16 or struct.unpack("<I", bs[36:40])[0]
        total = tot16 or tot32
        root_secs = -(-root_entries * 32 // self.bps)
        self.fat_off = self.base + reserved * self.bps
        self.root_off = self.fat_off + nfats * fatsz * self.bps
        self.root_len = root_secs * self.bps
        self.data_off = self.root_off + self.root_len
        self.cluster_bytes = self.spc * self.bps
        clusters = (total - reserved - nfats * fatsz - root_secs) // self.spc
        self.bits = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        self.root_cluster = struct.unpack("<I", bs[44:48])[0] if self.bits == 32 else 0
        self.fat = self._read(self.fat_off, fatsz * self.bps)

    @staticmethod
    def _looks_like_boot(sector):
        return sector[0] in (0xEB, 0xE9) and struct.unpack("<H", sector[11:13])[0] in (512, 1024, 2048, 4096)

    def _read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _next(self, c):
        if self.bits == 12:
            v = struct.unpack("<H", self.fat[c + c // 2:c + c // 2 + 2])[0]
            return v >> 4 if c & 1 else v & 0xFFF
        if self.bits == 16:
            return struct.unpack("<H", self.fat[2 * c:2 * c + 2])[0]
        return struct.unpack("<I", self.fat[4 * c:4 * c + 4])[0] & 0x0FFFFFFF

    def _chain(self, c):
        eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.bits]
        seen = set()
        while 2 <= c < eoc and c not in seen:
            seen.add(c)
            yield c
            c = self._next(c)

    def read_chain(self, c, size=None):
        out = bytearray()
        for cl in self._chain(c):
            out += self._read(self.data_off + (cl - 2) * self.cluster_bytes, self.cluster_bytes)
            if size is not None and len(out) >= size:
                break
        return bytes(out[:size]) if size is not None else bytes(out)

    def listdir(self, cluster):
        raw = self.read_chain(cluster) if cluster else (
            self.read_chain(self.root_cluster) if self.bits == 32 else self._read(self.root_off, self.root_len))
        lfn, lfn_sum = {}, None
        for i in range(0, len(raw), 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = {}
                continue
            if e[11] == ATTR_LFN:
                if e[0] & 0x40:
                    lfn = {}
                lfn_sum = e[13]
                lfn[e[0] & 0x1F] = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "replace")
                continue
            if e[11] & ATTR_VOLUME:
                lfn = {}
                continue
            base = e[0:8].replace(b"\x05", b"\xe5", 1).decode("cp437").rstrip()
            ext = e[8:11].decode("cp437").rstrip()
            if e[12] & 0x08:
                base = base.lower()
            if e[12] & 0x10:
                ext = ext.lower()
            name = base + ("." + ext if ext else "")
            if lfn and lfn_sum == lfn_checksum(e[0:11]) and sorted(lfn) == list(range(1, len(lfn) + 1)):
                name = "".join(lfn[k] for k in sorted(lfn)).split("\0")[0]
            lfn = {}
            if name in (".", ".."):
                continue
            hi, wtime, wdate, lo, size = struct.unpack("<HHHHI", e[20:32])
            yield {
                "name": name,
                "dir": bool(e[11] & ATTR_DIR),
                "cluster": ((hi << 16) if self.bits == 32 else 0) | lo,
                "size": size,
                "mtime": from_dos_datetime(wdate, wtime),
            }

    def walk(self, cluster=0, prefix=""):
        for e in self.listdir(cluster):
            path = prefix + "\\" + e["name"]
            yield path, e
            if e["dir"]:
                for sub in self.walk(e["cluster"], path):
                    yield sub


def cmd_list(args):
    vol = FatVolume(args.disk)
    for path, e in vol.walk():
        when = e["mtime"].strftime("%Y-%m-%d %H:%M") if e["mtime"] else "?"
        print("%s  %10s  D:%s" % (when, "<DIR>" if e["dir"] else e["size"], path))


def sha(data):
    return hashlib.sha1(data).hexdigest()


def cmd_pull(args):
    vol = FatVolume(args.disk)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    # Saves that were already moved on to saves-processed/ shouldn't come back.
    processed = {}
    for p in (REPO / "saves-processed").rglob("*"):
        if p.is_file():
            processed.setdefault(p.name.lower(), set()).add(sha(p.read_bytes()))

    found = {}
    for path, e in vol.walk():
        if not e["dir"] and e["name"].lower().endswith(".sav"):
            prev = found.get(e["name"].lower())
            if prev and (prev[1]["mtime"] or datetime.datetime.min) >= (e["mtime"] or datetime.datetime.min):
                print("  note: %s also at D:%s (kept the newer one)" % (e["name"], path))
                continue
            found[e["name"].lower()] = (path, e)

    counts = {"new": 0, "updated": 0, "same": 0, "processed": 0}
    for key in sorted(found):
        path, e = found[key]
        data = vol.read_chain(e["cluster"], e["size"]) if e["size"] else b""
        target = dest / e["name"]
        if target.exists():
            status = "same" if target.read_bytes() == data else "updated"
        elif sha(data) in processed.get(key, ()):
            status = "processed"
        else:
            status = "new"
        counts[status] += 1
        if status in ("new", "updated"):
            target.write_bytes(data)
            if e["mtime"]:
                ts = time.mktime(e["mtime"].timetuple())
                os.utime(str(target), (ts, ts))
            print("  %-7s %s" % (status, e["name"]))
    print("%d new, %d updated, %d unchanged, %d already in saves-processed -> %s"
          % (counts["new"], counts["updated"], counts["same"], counts["processed"], dest))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="create the VHD with the game and saves/")
    b.add_argument("--out", default=str(DEFAULT_DISK))
    b.add_argument("--force", action="store_true", help="overwrite an existing disk")
    l = sub.add_parser("list", help="list the files on the disk")
    l.add_argument("disk", nargs="?", default=str(DEFAULT_DISK))
    p = sub.add_parser("pull", help="copy *.sav files from the disk into saves/")
    p.add_argument("disk", nargs="?", default=str(DEFAULT_DISK))
    p.add_argument("--dest", default=str(REPO / "saves"))
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    {"build": cmd_build, "list": cmd_list, "pull": cmd_pull}[args.cmd](args)


if __name__ == "__main__":
    main()
