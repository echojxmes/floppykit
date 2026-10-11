#!/usr/bin/env python3
"""floppykit - identify, convert, list, extract and edit floppy disk images.

Formats    : raw sector images (.img/.ima/.dsk), ImageDisk (.imd, read-only)
Filesystem : FAT12 (8.3 names; long file names are skipped, never created)
Safety     : the input file is never modified; edits go to a new output file.

Usage:
  floppykit.py info    disk.img
  floppykit.py ls      disk.img [path] [-r]
  floppykit.py get     disk.img path [-o outdir]
  floppykit.py convert in.imd out.img [--fill 0xF6]
  floppykit.py new     out.img [--size 1440] [--label NAME]
  floppykit.py put     disk.img file... -o out.img [--dir PATH]
  floppykit.py mkdir   disk.img PATH... -o out.img
  floppykit.py rm      disk.img PATH... -o out.img
"""
import argparse
import datetime
import os
import re
import struct
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class FloppyKitError(Exception):
    pass


class SectorError(FloppyKitError):
    """A sector is missing or unreadable."""


class FormatError(FloppyKitError):
    """The data is not in the format we expected."""


# --------------------------------------------------------------------------
# Geometry and the image interface
# --------------------------------------------------------------------------
@dataclass
class Geometry:
    cylinders: int
    heads: int
    sectors_per_track: int
    sector_size: int = 512

    @property
    def total_sectors(self):
        return self.cylinders * self.heads * self.sectors_per_track


# total bytes -> (cylinders, heads, sectors per track); only a fallback,
# the BIOS Parameter Block wins whenever it looks sane.
STANDARD = {
    163840: (40, 1, 8),
    184320: (40, 1, 9),
    327680: (40, 2, 8),
    368640: (40, 2, 9),
    737280: (80, 2, 9),
    1228800: (80, 2, 15),
    1474560: (80, 2, 18),
    2949120: (80, 2, 36),
}


class DiskImage(ABC):
    geometry: Geometry
    format_name = "?"

    @abstractmethod
    def read_sector(self, c, h, s):
        """Sector numbers (s) are 1-based, as on real hardware."""

    def write_sector(self, c, h, s, data):
        raise FormatError(f"{self.format_name} images are read-only")

    def lba_to_chs(self, lba):
        g = self.geometry
        if not 0 <= lba < g.total_sectors:
            raise SectorError(f"LBA {lba} is outside the image")
        c, rem = divmod(lba, g.heads * g.sectors_per_track)
        h, s0 = divmod(rem, g.sectors_per_track)
        return c, h, s0 + 1

    def read_lba(self, lba):
        return self.read_sector(*self.lba_to_chs(lba))

    def write_lba(self, lba, data):
        self.write_sector(*self.lba_to_chs(lba), data)

    def bad_sectors(self):
        return []

    def warnings(self):
        return []


# --------------------------------------------------------------------------
# BIOS Parameter Block
# --------------------------------------------------------------------------
def parse_bpb(boot):
    if len(boot) < 36:
        return None
    (bps, spc, reserved, nfats, root_entries, total16, media, spf, spt,
     heads) = struct.unpack_from("<HBHBHHBHHH", boot, 11)
    total = total16 or struct.unpack_from("<I", boot, 32)[0]
    sane = (bps in (128, 256, 512, 1024, 2048, 4096)
            and spc in (1, 2, 4, 8, 16, 32, 64, 128)
            and reserved >= 1 and nfats in (1, 2) and root_entries > 0
            and total > 0 and spf > 0 and spt > 0 and heads > 0)
    return dict(bps=bps, spc=spc, reserved=reserved, fats=nfats,
                root_entries=root_entries, total=total, media=media,
                spf=spf, spt=spt, heads=heads, sane=sane)


# --------------------------------------------------------------------------
# Raw images
# --------------------------------------------------------------------------
class RawImage(DiskImage):
    format_name = "raw"

    def __init__(self, data):
        self.data = bytearray(data)
        self.geometry = self._detect()

    def _detect(self):
        n = len(self.data)
        bpb = parse_bpb(bytes(self.data[:512]))
        if bpb and bpb["sane"]:
            per_cyl = bpb["bps"] * bpb["spt"] * bpb["heads"]
            if n >= per_cyl and n % per_cyl == 0:
                return Geometry(n // per_cyl, bpb["heads"], bpb["spt"],
                                bpb["bps"])
        if n in STANDARD:
            return Geometry(*STANDARD[n])
        raise FormatError(
            f"cannot work out the geometry of a {n}-byte image "
            "(no usable BPB and not a standard floppy size)")

    def _offset(self, c, h, s):
        g = self.geometry
        if not (0 <= c < g.cylinders and 0 <= h < g.heads
                and 1 <= s <= g.sectors_per_track):
            raise SectorError(f"CHS {c}/{h}/{s} is outside the geometry")
        return ((c * g.heads + h) * g.sectors_per_track + (s - 1)) * g.sector_size

    def read_sector(self, c, h, s):
        o = self._offset(c, h, s)
        return bytes(self.data[o:o + self.geometry.sector_size])

    def write_sector(self, c, h, s, data):
        size = self.geometry.sector_size
        if len(data) != size:
            raise FloppyKitError(f"sector write must be {size} bytes")
        o = self._offset(c, h, s)
        self.data[o:o + size] = data


# --------------------------------------------------------------------------
# ImageDisk (.imd), read-only
# --------------------------------------------------------------------------
class ImdImage(DiskImage):
    format_name = "imd"
    SIZES = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192}

    def __init__(self, data):
        end = data.find(b"\x1a")
        if not data.startswith(b"IMD ") or end < 0:
            raise FormatError("not an ImageDisk file")
        self.comment = data[:end].decode("latin-1").strip()
        self.sectors = {}          # (cyl, head, sector id) -> bytes
        self._bad = []             # sectors flagged "data error"
        self._missing = []         # sectors the imager could not read
        self._warn = []
        pos, n = end + 1, len(data)
        track_info = {}
        while pos < n:
            if pos + 5 > n:
                raise FormatError("truncated IMD track header")
            mode, cyl, head, nsec, code = data[pos:pos + 5]
            pos += 5
            has_cmap, has_hmap = bool(head & 0x80), bool(head & 0x40)
            head &= 0x3F
            if code not in self.SIZES:
                raise FormatError(f"unsupported IMD sector size code {code}")
            size = self.SIZES[code]
            if pos + nsec > n:
                raise FormatError("truncated IMD sector map")
            smap = list(data[pos:pos + nsec])
            pos += nsec
            if has_cmap:
                pos += nsec        # logical cylinder map: ignored
            if has_hmap:
                pos += nsec        # logical head map: ignored
            for sid in smap:
                if pos >= n:
                    raise FormatError("truncated IMD track data")
                kind = data[pos]
                pos += 1
                key = (cyl, head, sid)
                if kind == 0:
                    self._missing.append(key)
                    continue
                if kind in (1, 3, 5, 7):
                    payload = data[pos:pos + size]
                    pos += size
                    if len(payload) != size:
                        raise FormatError("truncated IMD sector data")
                elif kind in (2, 4, 6, 8):
                    payload = bytes([data[pos]]) * size
                    pos += 1
                else:
                    raise FormatError(f"unknown IMD sector record type {kind}")
                if kind >= 5:
                    self._bad.append(key)
                self.sectors[key] = payload
            track_info[(cyl, head)] = (nsec, size)
        if not track_info:
            raise FormatError("IMD file contains no tracks")
        cyls = max(c for c, _ in track_info) + 1
        heads = max(h for _, h in track_info) + 1
        spt = max(v[0] for v in track_info.values())
        size = track_info[min(track_info)][1]
        self.geometry = Geometry(cyls, heads, spt, size)
        if len({v for v in track_info.values()}) > 1:
            self._warn.append("tracks differ in sector count or size; "
                              "LBA access assumes a uniform layout")
        if len(track_info) != cyls * heads:
            self._warn.append("some tracks are absent from the image")

    def read_sector(self, c, h, s):
        try:
            return self.sectors[(c, h, s)]
        except KeyError:
            raise SectorError(f"sector {c}/{h}/{s} is missing") from None

    def bad_sectors(self):
        return [f"{c}/{h}/{s} (data error flagged by imager)"
                for c, h, s in self._bad] + \
               [f"{c}/{h}/{s} (unreadable when imaged)"
                for c, h, s in self._missing]

    def warnings(self):
        return list(self._warn)


def open_image(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"IMD ":
        return ImdImage(data)
    if data[:8] == b"HXCPICFE":
        raise FormatError("HFE images are not supported yet")
    return RawImage(data)


def image_to_bytes(img, fill=0xF6):
    """Flatten any image to raw bytes. Returns (bytes, missing_sector_list)."""
    g = img.geometry
    out, missing = bytearray(), []
    pad = bytes([fill])
    for lba in range(g.total_sectors):
        try:
            sec = img.read_lba(lba)
        except SectorError:
            sec = b""
            missing.append(img.lba_to_chs(lba))
        out += sec[:g.sector_size].ljust(g.sector_size, pad)
    return bytes(out), missing


# --------------------------------------------------------------------------
# FAT12
# --------------------------------------------------------------------------
VALID_83 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


def to_83(name):
    up = name.upper()
    base, ext = up.rsplit(".", 1) if "." in up else (up, "")
    if (not base or len(base) > 8 or len(ext) > 3
            or any(ch not in VALID_83 for ch in base + ext)):
        raise FloppyKitError(f"'{name}' is not a valid 8.3 file name")
    return (base.ljust(8) + ext.ljust(3)).encode("cp437")


def dos_stamp(mtime=None):
    dt = datetime.datetime.fromtimestamp(mtime) if mtime else datetime.datetime.now()
    year = min(max(dt.year, 1980), 2107)
    t = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    d = ((year - 1980) << 9) | (dt.month << 5) | dt.day
    return t, d


def dos_to_datetime(date, tm):
    try:
        return datetime.datetime(1980 + (date >> 9), (date >> 5) & 15,
                                 date & 31, tm >> 11, (tm >> 5) & 63,
                                 (tm & 31) * 2)
    except ValueError:
        return None


@dataclass
class DirEntry:
    name: str
    attr: int
    cluster: int
    size: int
    mtime: object

    @property
    def is_dir(self):
        return bool(self.attr & 0x10)


class Fat12:
    def __init__(self, img):
        self.img = img
        self.boot = img.read_lba(0)
        b = parse_bpb(self.boot)
        if not b or not b["sane"]:
            raise FormatError("no valid FAT boot sector (BPB) found")
        if b["bps"] != img.geometry.sector_size:
            raise FormatError("BPB sector size differs from the image's")
        self.bpb = b
        self.bps, self.spc = b["bps"], b["spc"]
        self.fat_start, self.spf, self.nfats = b["reserved"], b["spf"], b["fats"]
        self.root_start = self.fat_start + self.nfats * self.spf
        self.root_sectors = (b["root_entries"] * 32 + self.bps - 1) // self.bps
        self.data_start = self.root_start + self.root_sectors
        self.n_clusters = (b["total"] - self.data_start) // self.spc
        if self.n_clusters <= 0:
            raise FormatError("BPB describes a filesystem with no data area")
        if self.n_clusters >= 4085:
            raise FormatError("FAT16/FAT32 is not supported (FAT12 only)")
        self.fat = self._read_fat(0)
        last = self.n_clusters + 1
        if len(self.fat) < last + last // 2 + 2:
            raise FormatError("FAT is too small for the cluster count")
        self.lfn_seen = False

    # ---- FAT table ------------------------------------------------------
    def _read_fat(self, copy):
        base = self.fat_start + copy * self.spf
        return bytearray(b"".join(self.img.read_lba(base + i)
                                  for i in range(self.spf)))

    def _flush_fat(self):
        for copy in range(self.nfats):
            base = self.fat_start + copy * self.spf
            for i in range(self.spf):
                self.img.write_lba(base + i,
                                   bytes(self.fat[i * self.bps:(i + 1) * self.bps]))

    def _get(self, n):
        o = n + n // 2
        v = self.fat[o] | (self.fat[o + 1] << 8)
        return (v >> 4) if n & 1 else (v & 0xFFF)

    def _set(self, n, val):
        o = n + n // 2
        v = self.fat[o] | (self.fat[o + 1] << 8)
        v = ((v & 0x000F) | (val << 4)) if n & 1 else ((v & 0xF000) | (val & 0xFFF))
        self.fat[o], self.fat[o + 1] = v & 0xFF, v >> 8

    def _chain(self, start):
        out, seen, c = [], set(), start
        while True:
            if c < 2 or c > self.n_clusters + 1:
                raise FormatError(f"cluster chain contains invalid cluster {c}")
            if c in seen:
                raise FormatError("cluster chain contains a loop")
            seen.add(c)
            out.append(c)
            nxt = self._get(c)
            if nxt >= 0xFF8:
                return out
            if nxt == 0xFF7:
                raise FormatError("cluster chain runs into a bad cluster")
            if nxt == 0:
                raise FormatError("cluster chain ends in a free cluster")
            c = nxt

    def _alloc(self, count):
        if count == 0:
            return []
        free = []
        for n in range(2, self.n_clusters + 2):
            if self._get(n) == 0:
                free.append(n)
                if len(free) == count:
                    return free
        raise FloppyKitError("disk full")

    def free_clusters(self):
        return sum(1 for n in range(2, self.n_clusters + 2) if self._get(n) == 0)

    # ---- cluster and directory I/O -------------------------------------
    def _read_cluster(self, n):
        lba = self.data_start + (n - 2) * self.spc
        return b"".join(self.img.read_lba(lba + i) for i in range(self.spc))

    def _write_cluster(self, n, data):
        lba = self.data_start + (n - 2) * self.spc
        for i in range(self.spc):
            self.img.write_lba(lba + i, data[i * self.bps:(i + 1) * self.bps])

    def _dir_bytes(self, cluster):
        if cluster is None:
            return bytearray(b"".join(self.img.read_lba(self.root_start + i)
                                      for i in range(self.root_sectors)))
        return bytearray(b"".join(self._read_cluster(c)
                                  for c in self._chain(cluster)))

    def _write_dir(self, cluster, data):
        if cluster is None:
            for i in range(self.root_sectors):
                self.img.write_lba(self.root_start + i,
                                   bytes(data[i * self.bps:(i + 1) * self.bps]))
        else:
            bpc = self.bps * self.spc
            for i, c in enumerate(self._chain(cluster)):
                self._write_cluster(c, bytes(data[i * bpc:(i + 1) * bpc]))

    def _entries(self, dirdata):
        """Yield (byte offset, DirEntry) for live 8.3 entries."""
        for i in range(0, len(dirdata), 32):
            e = dirdata[i:i + 32]
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                continue
            attr = e[11]
            if attr == 0x0F:
                self.lfn_seen = True
                continue
            if attr & 0x08 and not attr & 0x10:
                continue                       # volume label
            raw = bytes(e[0:8])
            if raw[0] == 0x05:
                raw = b"\xE5" + raw[1:]
            base = raw.decode("cp437").rstrip()
            ext = bytes(e[8:11]).decode("cp437").rstrip()
            tm, dt, cl, size = struct.unpack_from("<HHHI", e, 22)
            yield i, DirEntry(base + ("." + ext if ext else ""), attr, cl, size,
                              dos_to_datetime(dt, tm))

    @property
    def label(self):
        data = self._dir_bytes(None)
        for i in range(0, len(data), 32):
            if data[i] == 0x00:
                break
            if data[i] != 0xE5 and data[i + 11] & 0x08 and data[i + 11] != 0x0F:
                return bytes(data[i:i + 11]).decode("cp437").strip()
        return ""

    # ---- navigation -----------------------------------------------------
    def resolve(self, path):
        """Return (parent_dir_cluster, entry_offset, entry); root -> (None, None, None)."""
        parts = [p for p in re.split(r"[\\/]+", path) if p]
        cur, parent, off, entry = None, None, None, None
        for p in parts:
            if entry is not None and not entry.is_dir:
                raise NotADirectoryError(path)
            for o, e in self._entries(self._dir_bytes(cur)):
                if e.name.upper() == p.upper():
                    break
            else:
                raise FileNotFoundError(path)
            parent, off, entry = cur, o, e
            if e.is_dir:
                cur = e.cluster or None
        return parent, off, entry

    def walk(self, cluster=None, prefix="", _seen=None):
        _seen = _seen if _seen is not None else set()
        if cluster in _seen:
            return
        _seen.add(cluster)
        for _, e in self._entries(self._dir_bytes(cluster)):
            if e.name in (".", ".."):
                continue
            yield prefix + e.name, e
            if e.is_dir:
                yield from self.walk(e.cluster or None, prefix + e.name + "/", _seen)

    def listdir(self, path=""):
        entry = self.resolve(path)[2]
        if entry is None:
            cl = None
        elif entry.is_dir:
            cl = entry.cluster or None
        else:
            return [(entry.name, entry)]
        return [(e.name, e) for _, e in self._entries(self._dir_bytes(cl))
                if e.name not in (".", "..")]

    def read_file(self, entry):
        if entry.is_dir:
            raise IsADirectoryError(entry.name)
        if entry.size == 0:
            return b""
        data = b"".join(self._read_cluster(c) for c in self._chain(entry.cluster))
        if len(data) < entry.size:
            raise FormatError(f"{entry.name}: cluster chain is shorter than the "
                              "recorded file size")
        return data[:entry.size]

    # ---- modification ---------------------------------------------------
    def _prepare_slot(self, dir_cluster, name11):
        dirdata = self._dir_bytes(dir_cluster)
        slot = None
        for i in range(0, len(dirdata), 32):
            first = dirdata[i]
            if first == 0x00:
                if slot is None:
                    slot = i
                break
            if first == 0xE5:
                if slot is None:
                    slot = i
            elif dirdata[i + 11] != 0x0F and bytes(dirdata[i:i + 11]) == name11:
                n = name11.decode("cp437")
                raise FileExistsError(
                    f"'{(n[:8].rstrip() + '.' + n[8:].rstrip()).rstrip('.')}' "
                    "already exists in the target directory")
        if slot is None:
            raise FloppyKitError("directory is full (growing directories is not "
                               "supported)")
        return dirdata, slot

    def _commit_entry(self, dir_cluster, dirdata, slot, name11, attr, cluster,
                      size, mtime):
        t, d = dos_stamp(mtime)
        dirdata[slot:slot + 32] = name11 + struct.pack("<B10xHHHI", attr, t, d,
                                                       cluster, size)
        self._write_dir(dir_cluster, dirdata)

    def _dir_cluster_of(self, path):
        entry = self.resolve(path)[2]
        if entry is None:
            return None
        if not entry.is_dir:
            raise NotADirectoryError(path)
        return entry.cluster or None

    def put(self, dest_dir, name, data, mtime=None):
        dir_cluster = self._dir_cluster_of(dest_dir)
        name11 = to_83(name)
        dirdata, slot = self._prepare_slot(dir_cluster, name11)
        bpc = self.bps * self.spc
        need = -(-len(data) // bpc)
        clusters = self._alloc(need)
        for a, b in zip(clusters, clusters[1:]):
            self._set(a, b)
        if clusters:
            self._set(clusters[-1], 0xFFF)
        padded = data.ljust(need * bpc, b"\x00")
        for i, c in enumerate(clusters):
            self._write_cluster(c, padded[i * bpc:(i + 1) * bpc])
        self._commit_entry(dir_cluster, dirdata, slot, name11, 0x20,
                           clusters[0] if clusters else 0, len(data), mtime)
        self._flush_fat()

    def mkdir(self, parent_path, name, mtime=None):
        pcl = self._dir_cluster_of(parent_path)
        name11 = to_83(name)
        dirdata, slot = self._prepare_slot(pcl, name11)
        (c,) = self._alloc(1)
        self._set(c, 0xFFF)
        t, d = dos_stamp(mtime)
        block = bytearray(self.bps * self.spc)
        for i, (nm, target) in enumerate(((b".          ", c),
                                          (b"..         ", pcl or 0))):
            block[i * 32:(i + 1) * 32] = nm + struct.pack("<B10xHHHI", 0x10, t, d,
                                                          target, 0)
        self._write_cluster(c, bytes(block))
        self._commit_entry(pcl, dirdata, slot, name11, 0x10, c, 0, mtime)
        self._flush_fat()

    def rm(self, path):
        parent, off, entry = self.resolve(path)
        if entry is None:
            raise FloppyKitError("cannot remove the root directory")
        if entry.is_dir and any(True for _ in self.walk(entry.cluster or None)):
            raise FloppyKitError(f"directory '{path}' is not empty")
        if entry.cluster >= 2:
            for c in self._chain(entry.cluster):
                self._set(c, 0)
        dirdata = self._dir_bytes(parent)
        dirdata[off] = 0xE5
        self._write_dir(parent, dirdata)
        self._flush_fat()

    # ---- hazard report --------------------------------------------------
    def check(self):
        out, b, g = [], self.bpb, self.img.geometry
        if (b["spt"], b["heads"]) != (g.sectors_per_track, g.heads):
            out.append(f"BPB geometry ({b['heads']} heads, {b['spt']} sectors/track) "
                       f"differs from the image's ({g.heads}, {g.sectors_per_track})")
        if b["total"] > g.total_sectors:
            out.append("BPB claims more sectors than the image contains")
        for copy in range(1, self.nfats):
            try:
                if self._read_fat(copy) != self.fat:
                    out.append(f"FAT copy {copy + 1} differs from FAT copy 1")
            except SectorError:
                out.append(f"FAT copy {copy + 1} is unreadable")
        if self.fat[0] != b["media"]:
            out.append(f"media descriptor in BPB (0x{b['media']:02X}) does not "
                       f"match FAT (0x{self.fat[0]:02X})")
        if self.boot[510:512] != b"\x55\xAA":
            out.append("boot sector lacks the 0x55AA signature (normal for very "
                       "old disks)")
        try:
            list(self.walk())
        except FloppyKitError as e:
            out.append(f"directory tree problem: {e}")
        if self.lfn_seen:
            out.append("long file name entries present; they are skipped, so "
                       "files are shown by their 8.3 names")
        return out


# --------------------------------------------------------------------------
# New blank images
# --------------------------------------------------------------------------
# KB -> (cylinders, heads, spt, sectors/cluster, root entries, media, sectors/FAT)
FORMATS = {
    160: (40, 1, 8, 1, 64, 0xFE, 1),
    180: (40, 1, 9, 1, 64, 0xFC, 2),
    320: (40, 2, 8, 2, 112, 0xFF, 1),
    360: (40, 2, 9, 2, 112, 0xFD, 2),
    720: (80, 2, 9, 2, 112, 0xF9, 3),
    1200: (80, 2, 15, 1, 224, 0xF9, 7),
    1440: (80, 2, 18, 1, 224, 0xF0, 9),
    2880: (80, 2, 36, 2, 240, 0xF0, 9),
}


def new_image(kb=1440, label=""):
    if kb not in FORMATS:
        raise FloppyKitError(f"unsupported size {kb}; choose from {sorted(FORMATS)}")
    cyl, heads, spt, spc, root, media, spf = FORMATS[kb]
    total = cyl * heads * spt
    img = bytearray(total * 512)
    img[0:3] = b"\xEB\x3C\x90"
    img[3:11] = b"FLOPKIT "
    struct.pack_into("<HBHBHHBHHHII", img, 11, 512, spc, 1, 2, root, total,
                     media, spf, spt, heads, 0, 0)
    img[38] = 0x29
    struct.pack_into("<I", img, 39, int(time.time()) & 0xFFFFFFFF)
    lab = label.upper()[:11].ljust(11)
    img[43:54] = lab.encode("cp437", "replace")
    img[54:62] = b"FAT12   "
    img[510:512] = b"\x55\xAA"
    for copy in range(2):
        base = (1 + copy * spf) * 512
        img[base:base + 3] = bytes([media, 0xFF, 0xFF])
    if label:
        t, d = dos_stamp()
        root_off = (1 + 2 * spf) * 512
        img[root_off:root_off + 32] = lab.encode("cp437", "replace") + \
            struct.pack("<B10xHHHI", 0x08, t, d, 0, 0)
    return RawImage(img)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def fmt_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "----------------"


def load_writable(path):
    img = open_image(path)
    if isinstance(img, RawImage):
        return img
    data, missing = image_to_bytes(img)
    print(f"note: {img.format_name} is read-only; editing a raw copy "
          f"({len(missing)} missing sectors filled)", file=sys.stderr)
    return RawImage(data)


def save_image(img, out, src=None):
    if src and os.path.exists(out) and os.path.samefile(src, out):
        raise FloppyKitError("output must differ from the input; "
                           "floppykit never edits images in place")
    with open(out, "wb") as f:
        f.write(img.data)


def cmd_info(a):
    img = open_image(a.image)
    g = img.geometry
    print(f"format      : {img.format_name}")
    print(f"geometry    : {g.cylinders} cylinders, {g.heads} heads, "
          f"{g.sectors_per_track} sectors/track, {g.sector_size} bytes/sector")
    print(f"capacity    : {g.total_sectors * g.sector_size // 1024} KB")
    findings = list(img.warnings()) + \
        [f"bad sector {s}" for s in img.bad_sectors()]
    try:
        fs = Fat12(img)
        free = fs.free_clusters()
        print(f"filesystem  : FAT12, OEM '{fs.boot[3:11].decode('cp437').strip()}', "
              f"label '{fs.label}'")
        print(f"clusters    : {fs.n_clusters} x {fs.bps * fs.spc} bytes, "
              f"{free} free ({free * fs.bps * fs.spc // 1024} KB)")
        findings += fs.check()
    except (FormatError, SectorError) as e:
        print(f"filesystem  : not recognised as FAT12 ({e})")
    print("findings    : " + ("none" if not findings else ""))
    for f in findings:
        print(f"  - {f}")


def cmd_ls(a):
    fs = Fat12(open_image(a.image))
    entry = fs.resolve(a.path)[2]
    if a.recursive and (entry is None or entry.is_dir):
        base = a.path.strip("/\\")
        cl = entry.cluster or None if entry else None
        items = [((base + "/" if base else "") + p, e) for p, e in fs.walk(cl)]
    else:
        items = fs.listdir(a.path)
    for p, e in items:
        size = "<DIR>" if e.is_dir else str(e.size)
        print(f"{size:>10}  {fmt_time(e.mtime)}  {p}{'/' if e.is_dir else ''}")
    if fs.lfn_seen:
        print("note: long file names present; showing 8.3 names", file=sys.stderr)


def cmd_get(a):
    fs = Fat12(open_image(a.image))
    out = a.output or "."
    entry = fs.resolve(a.path)[2]
    if entry is None:
        items = list(fs.walk())
    elif entry.is_dir:
        items = [(entry.name, entry)] + \
            [(entry.name + "/" + p, e) for p, e in fs.walk(entry.cluster or None)]
    else:
        items = [(entry.name, entry)]
    root = os.path.realpath(out)
    for p, e in items:
        dest = os.path.realpath(os.path.join(out, *p.split("/")))
        if not dest.startswith(root + os.sep) and dest != root:
            raise FloppyKitError(f"refusing to write outside {out}: {p}")
        if e.is_dir:
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(fs.read_file(e))
        if e.mtime:
            ts = e.mtime.timestamp()
            os.utime(dest, (ts, ts))
        print(f"extracted {p} ({e.size} bytes)")


def cmd_convert(a):
    img = open_image(a.input)
    data, missing = image_to_bytes(img, int(a.fill, 0))
    if os.path.exists(a.output) and os.path.samefile(a.input, a.output):
        raise FloppyKitError("output must differ from the input")
    with open(a.output, "wb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes to {a.output}")
    if missing:
        print(f"warning: {len(missing)} sectors were unreadable and filled with "
              f"0x{int(a.fill, 0):02X}", file=sys.stderr)


def cmd_new(a):
    img = new_image(a.size, a.label or "")
    with open(a.output, "wb") as f:
        f.write(img.data)
    print(f"created empty {a.size} KB FAT12 image: {a.output} (not bootable)")


def cmd_put(a):
    img = load_writable(a.image)
    fs = Fat12(img)
    for path in a.files:
        with open(path, "rb") as f:
            data = f.read()
        fs.put(a.dir, os.path.basename(path), data, os.path.getmtime(path))
        print(f"added {os.path.basename(path)} ({len(data)} bytes)")
    save_image(img, a.output, a.image)


def cmd_mkdir(a):
    img = load_writable(a.image)
    fs = Fat12(img)
    for path in a.paths:
        parts = [p for p in re.split(r"[\\/]+", path) if p]
        fs.mkdir("/".join(parts[:-1]), parts[-1])
        print(f"created {path}")
    save_image(img, a.output, a.image)


def cmd_rm(a):
    img = load_writable(a.image)
    fs = Fat12(img)
    for path in a.paths:
        fs.rm(path)
        print(f"removed {path}")
    save_image(img, a.output, a.image)


def build_parser():
    p = argparse.ArgumentParser(prog="floppykit", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="detect format, geometry, filesystem; report problems")
    s.add_argument("image")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("ls", help="list files")
    s.add_argument("image")
    s.add_argument("path", nargs="?", default="")
    s.add_argument("-r", "--recursive", action="store_true")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("get", help="extract a file or directory tree")
    s.add_argument("image")
    s.add_argument("path", help="path inside the image ('/' for everything)")
    s.add_argument("-o", "--output", help="destination directory")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("convert", help="convert any readable image to a raw image")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--fill", default="0xF6", help="byte for unreadable sectors")
    s.set_defaults(fn=cmd_convert)

    s = sub.add_parser("new", help="create an empty FAT12 floppy image")
    s.add_argument("output")
    s.add_argument("--size", type=int, default=1440, choices=sorted(FORMATS))
    s.add_argument("--label")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("put", help="add files (written to a new image)")
    s.add_argument("image")
    s.add_argument("files", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--dir", default="", help="target directory inside the image")
    s.set_defaults(fn=cmd_put)

    s = sub.add_parser("mkdir", help="create directories (written to a new image)")
    s.add_argument("image")
    s.add_argument("paths", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_mkdir)

    s = sub.add_parser("rm", help="delete files or empty dirs (written to a new image)")
    s.add_argument("image")
    s.add_argument("paths", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_rm)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (FloppyKitError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())