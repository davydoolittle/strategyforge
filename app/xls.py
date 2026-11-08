"""
Minimal reader for legacy Excel (.xls, BIFF8) files, standard library only.

Enough for one job: reading the numbers and labels of a sheet in Robert
Shiller's ie_data.xls, so the repo still needs no pip packages. It reads the
OLE2 container, the shared-string table and the cell records NUMBER, RK,
MULRK, LABELSST and FORMULA (the cached numeric result). Dates, styles and
everything else are ignored. It is checked against xlrd on the real file in
tests/test_market_context.py.

  cells = read_sheet(open("ie_data.xls", "rb").read(), "Data")
  cells[(row, col)] -> float or str
"""

import struct

ENDOFCHAIN = 0xFFFFFFFE


class XlsError(ValueError):
    pass


def _stream(data, name):
    """The named stream of an OLE2 compound file."""
    if data[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
        raise XlsError("not an OLE2 (.xls) file")
    ssz = 1 << struct.unpack_from("<H", data, 30)[0]
    mini_cut = struct.unpack_from("<I", data, 56)[0]
    dir_start = struct.unpack_from("<I", data, 48)[0]
    minifat_start, n_minifat = struct.unpack_from("<II", data, 60)
    difat_start, n_difat = struct.unpack_from("<II", data, 68)

    def sector(i):
        return data[512 + i * ssz: 512 + (i + 1) * ssz]

    difat = [x for x in struct.unpack_from("<109I", data, 76) if x < ENDOFCHAIN]
    nxt = difat_start
    for _ in range(n_difat):
        vals = struct.unpack(f"<{ssz // 4}I", sector(nxt))
        difat += [x for x in vals[:-1] if x < ENDOFCHAIN]
        nxt = vals[-1]
    fat = []
    for s in difat:
        fat += struct.unpack(f"<{ssz // 4}I", sector(s))

    def chain(start):
        out, i, seen = [], start, set()
        while i < ENDOFCHAIN:
            if i in seen or i >= len(fat):
                raise XlsError("corrupt sector chain")
            seen.add(i)
            out.append(i)
            i = fat[i]
        return out

    dirs = b"".join(sector(i) for i in chain(dir_start))
    root = None
    for off in range(0, len(dirs), 128):
        e = dirs[off:off + 128]
        nlen = struct.unpack_from("<H", e, 64)[0]
        ename = e[:max(nlen - 2, 0)].decode("utf-16-le", "replace")
        etype = e[66]
        start, size = struct.unpack_from("<II", e, 116)
        if etype == 5:
            root = (start, size)
        if ename == name and etype == 2:
            if size >= mini_cut:
                return b"".join(sector(i) for i in chain(start))[:size]
            ministream = b"".join(sector(i) for i in chain(root[0]))
            minifat = []
            if n_minifat:
                for s in chain(minifat_start):
                    minifat += struct.unpack(f"<{ssz // 4}I", sector(s))
            out, i = [], start
            while i < ENDOFCHAIN:
                out.append(ministream[i * 64:(i + 1) * 64])
                i = minifat[i]
            return b"".join(out)[:size]
    raise XlsError(f"no {name} stream")


def _records(wb, pos=0):
    while pos + 4 <= len(wb):
        rtype, rlen = struct.unpack_from("<HH", wb, pos)
        yield pos, rtype, wb[pos + 4: pos + 4 + rlen]
        pos += 4 + rlen


def _rk(v):
    """An RK number: bit 1 says a 30-bit signed integer, else the top 30 bits of a
    double; bit 0 says divide by 100."""
    if v & 2:
        x = float(struct.unpack("<i", struct.pack("<I", v))[0] >> 2)
    else:
        x = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return x / 100.0 if v & 1 else x


class _Sst:
    """Shared strings. A string may run across CONTINUE records; each new
    record starts with a fresh flag byte for the characters that follow."""

    def __init__(self, parts):
        self.parts, self.pi, self.pos = parts, 0, 8
        self.count = struct.unpack_from("<I", parts[0], 4)[0]

    def _need(self, n):
        if self.pos + n > len(self.parts[self.pi]):
            self.pi += 1
            self.pos = 0

    def _take(self, n):
        self._need(n)
        b = self.parts[self.pi][self.pos:self.pos + n]
        self.pos += n
        return b

    def strings(self):
        out = []
        for _ in range(self.count):
            nchars = struct.unpack("<H", self._take(2))[0]
            flags = self._take(1)[0]
            runs = struct.unpack("<H", self._take(2))[0] if flags & 8 else 0
            ext = struct.unpack("<I", self._take(4))[0] if flags & 4 else 0
            text, wide = "", flags & 1
            while len(text) < nchars:
                if self.pos >= len(self.parts[self.pi]):
                    self.pi += 1                              # CONTINUE: a new flag byte
                    wide = self.parts[self.pi][0] & 1
                    self.pos = 1
                part = self.parts[self.pi]
                width = 2 if wide else 1
                n = min(nchars - len(text), (len(part) - self.pos) // width)
                raw = part[self.pos:self.pos + n * width]
                text += raw.decode("utf-16-le") if wide else raw.decode("latin-1")
                self.pos += n * width
            for skip in (runs * 4, ext):
                while skip:
                    part = self.parts[self.pi]
                    step = min(skip, len(part) - self.pos)
                    self.pos += step
                    skip -= step
                    if skip:
                        self.pi += 1
                        self.pos = 0
            out.append(text)
        return out


def read_sheet(data, sheet):
    wb = _stream(data, "Workbook")
    sheets, sst, parts = {}, [], None
    for pos, rtype, body in _records(wb):
        if rtype == 0x0085:                                   # BOUNDSHEET
            off = struct.unpack_from("<I", body, 0)[0]
            n, wide = body[6], body[7] & 1
            name = body[8:8 + n * (2 if wide else 1)].decode("utf-16-le" if wide else "latin-1")
            sheets[name] = off
        elif rtype == 0x00FC:                                 # SST
            parts = [body]
        elif rtype == 0x003C and parts is not None:           # CONTINUE of the SST
            parts.append(body)
        elif parts is not None:
            sst = _Sst(parts).strings()
            parts = None
        if rtype == 0x000A:                                   # EOF of the workbook globals
            break
    if sheet not in sheets:
        raise XlsError(f"no sheet {sheet!r}; have {sorted(sheets)}")
    cells = {}
    for pos, rtype, body in _records(wb, sheets[sheet]):
        if rtype == 0x0203:                                   # NUMBER
            r, c = struct.unpack_from("<HH", body)
            cells[(r, c)] = struct.unpack_from("<d", body, 6)[0]
        elif rtype == 0x027E:                                 # RK
            r, c = struct.unpack_from("<HH", body)
            cells[(r, c)] = _rk(struct.unpack_from("<I", body, 6)[0])
        elif rtype == 0x00BD:                                 # MULRK
            r, c = struct.unpack_from("<HH", body)
            for k in range((len(body) - 6) // 6):
                cells[(r, c + k)] = _rk(struct.unpack_from("<I", body, 4 + k * 6 + 2)[0])
        elif rtype == 0x00FD:                                 # LABELSST
            r, c = struct.unpack_from("<HH", body)
            cells[(r, c)] = sst[struct.unpack_from("<I", body, 6)[0]]
        elif rtype == 0x0006:                                 # FORMULA: numeric cached result only
            r, c = struct.unpack_from("<HH", body)
            if body[12:14] != b"\xff\xff":
                cells[(r, c)] = struct.unpack_from("<d", body, 6)[0]
        elif rtype == 0x000A:                                 # EOF of this sheet
            break
    return cells
