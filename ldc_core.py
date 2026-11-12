"""LDCompact core - doc/phan tich/nen VMDK cua LDPlayer.

Nguyen ly: VMDK sparse = header + descriptor + RGD + RGT + GD + GT + grains.
Moi grain 64 KB tuong ung 16 block ext4 4 KB. Grain chi duoc bo khi CA 16 block
deu nam trong block bitmap cua ext4 o trang thai free.

Khi nen: giu nguyen toan bo metadata (header/descriptor/RGD/GD) va vi tri cac
sector, chi doi gia tri GTE -> grain duoc don ve dau, file duoc cat ngan.
GT va RGT luon duoc ghi giong het nhau (VirtualBox kiem tra cheo).
"""
import os
import struct
import hashlib
from array import array

GB = 1024 ** 3
SECTOR = 512


class VmdkError(Exception):
    pass


class Vmdk:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        h = self.f.read(512)
        if len(h) < 512:
            raise VmdkError("file qua ngan")
        self.magic, self.version, self.flags = struct.unpack("<III", h[:12])
        if self.magic != 0x564D444B:
            raise VmdkError("khong phai VMDK sparse (magic=%#x)" % self.magic)
        (self.capacity, self.grain, self.desc_off, self.desc_size) = struct.unpack("<QQQQ", h[12:44])
        self.ngte = struct.unpack("<I", h[44:48])[0]
        (self.rgd, self.gd, self.overhead) = struct.unpack("<QQQ", h[48:72])
        self.compress = struct.unpack("<H", h[77:79])[0]
        self.grain_bytes = self.grain * SECTOR
        self.total_grains = self.capacity * SECTOR // self.grain_bytes
        self.gts = (self.total_grains + self.ngte - 1) // self.ngte
        self.header = h
        self._load()

    def _load(self):
        self.f.seek(self.gd * SECTOR)
        gd = self.f.read(self.gts * 4)
        self.gd_entries = array("I", gd[: self.gts * 4])
        self.f.seek(self.rgd * SECTOR)
        rgd = self.f.read(self.gts * 4)
        self.rgd_entries = array("I", rgd[: self.gts * 4])
        self.gte = array("I", bytes(4 * self.total_grains))
        gt_sectors = self.ngte * 4 // SECTOR
        i = 0
        while i < self.gts:
            sec = self.gd_entries[i]
            if not sec:
                i += 1
                continue
            j = i + 1
            while j < self.gts and self.gd_entries[j] == sec + (j - i) * gt_sectors:
                j += 1
            cnt = j - i
            self.f.seek(sec * SECTOR)
            buf = self.f.read(cnt * self.ngte * 4)
            self.gte[i * self.ngte:(i + cnt) * self.ngte] = array(
                "I", buf[:cnt * self.ngte * 4])
            i = j

    def read_at(self, vaddr, length):
        out = bytearray()
        pos, end = vaddr, vaddr + length
        while pos < end:
            g = pos // self.grain_bytes
            off = pos % self.grain_bytes
            gte = self.gte[g]
            n = min(self.grain_bytes - off, end - pos)
            if gte == 0:
                out += b"\x00" * n
            else:
                self.f.seek(gte * SECTOR + off)
                out += self.f.read(n)
            pos += n
        return bytes(out)

    def close(self):
        self.f.close()


# ---------- ext4 ----------

def _parts(v):
    head = v.read_at(0, 512)
    if head[510:512] != b"\x55\xaa":
        return [(0, v.capacity * SECTOR)]
    out = []
    for i in range(4):
        e = head[446 + i * 16:446 + (i + 1) * 16]
        if e[4] in (0x83, 0x07, 0x0B, 0x0C, 0x05, 0x0F):
            lba = struct.unpack("<I", e[8:12])[0]
            n = struct.unpack("<I", e[12:16])[0]
            if n:
                out.append((lba * SECTOR, n * SECTOR))
    return out or [(0, v.capacity * SECTOR)]


def _ext4(v, off):
    sb = v.read_at(off + 1024, 1024)
    if len(sb) < 1024 or struct.unpack("<H", sb[56:58])[0] != 0xEF53:
        return None
    u32 = lambda o: struct.unpack("<I", sb[o:o + 4])[0]
    u16 = lambda o: struct.unpack("<H", sb[o:o + 2])[0]
    return dict(blocks=u32(4), fdb=u32(20), log_bs=u32(24), bpg=u32(32),
                feat=u32(96), desc=u16(0xFE) or 32, state=u16(58),
                label=sb[0x78:0x88].rstrip(b"\x00").decode("ascii", "replace"))


def free_grains(v):
    """Tra ve (set grain free, list ghi chu, thong ke)."""
    gb = v.grain_bytes
    notes, found = [], set()
    for poff, _plen in _parts(v):
        info = _ext4(v, poff)
        if not info:
            continue
        bs = 1024 << info["log_bs"]
        blocks = info["blocks"]
        bpg = info["bpg"]
        nb = gb // bs                      # so block trong 1 grain (16)
        is64 = bool(info["feat"] & 0x80)
        groups = (blocks - info["fdb"] + bpg - 1) // bpg
        gd = v.read_at(poff + (info["fdb"] + 1) * bs, groups * info["desc"])
        aligned = (bpg % nb == 0)
        fg = poff // gb
        nfree_blocks = 0
        for g in range(groups):
            d = gd[g * info["desc"]:(g + 1) * info["desc"]]
            bmp = struct.unpack("<I", d[0:4])[0]
            if is64 and info["desc"] >= 64:
                bmp |= struct.unpack("<I", d[0x20:0x24])[0] << 32
            start = g * bpg
            nblk = min(bpg, blocks - start)
            if not bmp:
                nfree_blocks += nblk
                if aligned:
                    base = fg + start // nb
                    for k in range(nblk // nb):
                        found.add(base + k)
                continue
            raw = v.read_at(poff + bmp * bs, bs)
            used = int.from_bytes(raw, "little") & ((1 << nblk) - 1)
            nfree_blocks += nblk - used.bit_count()
            if not aligned or poff % gb:
                continue
            # moi grain = 16 block = 2 byte lien tiep trong bitmap;
            # ca 2 byte bang 0 => ca 16 block deu free
            nfull = nblk // nb
            vals = array("H", raw[:nfull * 2])
            base = fg + start // nb
            for k in range(nfull):
                if not vals[k]:
                    found.add(base + k)
        notes.append(dict(offset=poff, fs_gb=blocks * bs / GB,
                          free_gb=nfree_blocks * bs / GB, state=info["state"]))
        if poff % gb:
            notes.append(dict(offset=poff, warn="partition lech grain, bo qua"))
    return found, notes


_CACHE = {}


def analyze(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    v = Vmdk(path)
    try:
        fg, notes = free_grains(v)
        alloc = sum(1 for x in v.gte if x)
        fg = {g for g in fg if v.gte[g] != 0}
        states = [n["state"] for n in notes if "state" in n]
        res = dict(path=path, size=st.st_size, alloc=alloc,
                   keep=alloc - len(fg), reclaim=len(fg), grain=v.grain_bytes,
                   total_grains=v.total_grains, overhead=v.overhead,
                   states=states, clean=all(s == 1 for s in states) if states else False,
                   notes=notes, ok=True)
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = res
        return res
    finally:
        v.close()


def read_header(path):
    """Chi doc 512 byte header, khong nap grain table (nhanh)."""
    with open(path, "rb") as f:
        h = f.read(512)
    if len(h) < 512:
        raise VmdkError("file qua ngan")
    magic, ver, flags = struct.unpack("<III", h[:12])
    if magic != 0x564D444B:
        raise VmdkError("khong phai VMDK sparse (magic=%#x)" % magic)
    cap, grain, doff, dsz = struct.unpack("<QQQQ", h[12:44])
    ngte = struct.unpack("<I", h[44:48])[0]
    rgd, gd, oh = struct.unpack("<QQQ", h[48:72])
    return dict(version=ver, flags=flags, capacity=cap, grain=grain, ngte=ngte,
                overhead=oh, compress=struct.unpack("<H", h[77:79])[0],
                desc_off=doff, desc_size=dsz, rgd=rgd, gd=gd,
                size=os.path.getsize(path))


def header_info(path):
    """Doc thong tin header de guard (khong nap grain table)."""
    return read_header(path)


# ---------- compact ----------

def compact(src, dst, progress=None, verify=True):
    v = Vmdk(src)
    try:
        gb = v.grain_bytes
        fg, notes = free_grains(v)
        fg = {g for g in fg if v.gte[g] != 0}
        keep = [g for g in range(v.total_grains) if v.gte[g] and g not in fg]
        new_total = v.overhead * SECTOR + len(keep) * gb

        v.f.seek(0)
        meta = bytearray(v.f.read(v.overhead * SECTOR))
        if len(meta) != v.overhead * SECTOR:
            raise VmdkError("khong doc duoc vung metadata")

        new_gt = array("I", bytes(4 * v.total_grains))
        pos = v.overhead
        for g in keep:
            new_gt[g] = pos
            pos += v.grain

        # ghi GT va RGT giong het nhau (ke ca block rong -> phai ghi zero)
        for i in range(v.gts):
            block = new_gt[i * v.ngte:(i + 1) * v.ngte].tobytes()
            sec = v.gd_entries[i]
            if sec:
                meta[sec * SECTOR: sec * SECTOR + len(block)] = block
            rsec = v.rgd_entries[i]
            if rsec:
                meta[rsec * SECTOR: rsec * SECTOR + len(block)] = block

        with open(dst, "wb") as out:
            out.write(meta)
            digests = []
            for n, g in enumerate(keep):
                v.f.seek(v.gte[g] * SECTOR)
                buf = v.f.read(gb)
                if len(buf) != gb:
                    raise VmdkError("doc thieu grain %d" % g)
                if verify:
                    digests.append(hashlib.blake2b(buf, digest_size=8).digest())
                out.write(buf)
                if progress and n % 512 == 0:
                    progress(n, len(keep))
            out.flush()
            os.fsync(out.fileno())

        real = os.path.getsize(dst)
        if real != new_total:
            raise VmdkError("kich thuoc sai: %d != %d" % (real, new_total))

        if verify:
            _verify(src, dst, keep, digests, progress)

        return dict(src=src, dst=dst, old=os.path.getsize(src), new=new_total,
                    grains=len(keep), dropped=len(fg), notes=notes)
    finally:
        v.close()


def _verify(src, dst, keep, digests, progress=None):
    a, b = Vmdk(src), Vmdk(dst)
    try:
        gb = a.grain_bytes
        for n, g in enumerate(keep):
            a.f.seek(a.gte[g] * SECTOR)
            x = a.f.read(gb)
            b.f.seek(b.gte[g] * SECTOR)
            y = b.f.read(gb)
            if x != y:
                raise VmdkError("grain %d khac nhau sau khi ghi" % g)
            if progress and n % 512 == 0:
                progress(n, len(keep))
        if len(b.gte) != len(a.gte):
            raise VmdkError("so grain lech")
    finally:
        a.close()
        b.close()


def validate(path):
    """Kiem tra cau truc VMDK sau khi nen. Tra ve list loi (rong = OK)."""
    errs = []
    v = Vmdk(path)
    try:
        size = os.path.getsize(path)
        gb = v.grain_bytes
        if size % gb:
            errs.append("kich thuoc file khong chia het grain (%d)" % size)
        if v.overhead * SECTOR > size:
            errs.append("overhead vuot qua kich thuoc file")
        n_alloc = 0
        for g in range(v.total_grains):
            s = v.gte[g]
            if not s:
                continue
            n_alloc += 1
            if s < v.overhead:
                errs.append("grain %d tro vao vung metadata (sector %d)" % (g, s))
                break
            if (s + v.grain) * SECTOR > size:
                errs.append("grain %d tro ra ngoai file" % g)
                break
        if size != v.overhead * SECTOR + n_alloc * gb:
            errs.append("kich thuoc khong khop so grain: %d vs %d"
                        % (size, v.overhead * SECTOR + n_alloc * gb))
        # GT phai giong RGT tung byte
        for i in range(v.gts):
            if not v.gd_entries[i] and not v.rgd_entries[i]:
                continue
            if not v.gd_entries[i] or not v.rgd_entries[i]:
                errs.append("GT %d thieu ban sao RGT" % i)
                break
            v.f.seek(v.gd_entries[i] * SECTOR)
            a = v.f.read(v.ngte * 4)
            v.f.seek(v.rgd_entries[i] * SECTOR)
            b = v.f.read(v.ngte * 4)
            if a != b:
                errs.append("GT %d khac RGT" % i)
                break
        # descriptor con nguyen
        v.f.seek(v.desc_off * SECTOR)
        desc = v.f.read(v.desc_size * SECTOR)
        if b"createType" not in desc or b"RW " not in desc:
            errs.append("descriptor hong")
        return errs
    finally:
        v.close()
