from __future__ import annotations

import struct
from dataclasses import dataclass


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.index = 0

    def u8(self) -> int:
        value = self.data[self.index]
        self.index += 1
        return value

    def u16(self) -> int:
        value = struct.unpack_from(">H", self.data, self.index)[0]
        self.index += 2
        return value

    def i16(self) -> int:
        value = struct.unpack_from(">h", self.data, self.index)[0]
        self.index += 2
        return value

    def u32(self) -> int:
        value = struct.unpack_from(">I", self.data, self.index)[0]
        self.index += 4
        return value

    def u64(self) -> int:
        value = struct.unpack_from(">Q", self.data, self.index)[0]
        self.index += 8
        return value

    def take(self, size: int) -> bytes:
        value = self.data[self.index:self.index + size]
        if len(value) != size:
            raise IndexError("font data ended unexpectedly")
        self.index += size
        return value


@dataclass
class HistoricalQueryTTF:
    """Python port of Legado 3.23.110211 QueryTTF.java, quirks included."""

    raw: bytes

    def __post_init__(self) -> None:
        reader = _Reader(self.raw)
        reader.take(4)
        table_count = reader.u16()
        reader.take(6)
        tables: dict[str, tuple[int, int]] = {}
        for _ in range(table_count):
            tag = reader.take(4).decode("ascii")
            reader.u32()
            tables[tag] = (reader.u32(), reader.u32())

        self._reader = reader
        self._tables = tables
        self._names: list[tuple[int, int, int, int, int, int]] = []
        self._name_header = (0, 0)
        if "name" in tables:
            offset, _ = tables["name"]
            reader.index = offset
            reader.u16()
            count, string_offset = reader.u16(), reader.u16()
            self._name_header = (offset, string_offset)
            self._names = [tuple(reader.u16() for _ in range(6)) for _ in range(count)]

        loca_format = 0
        if "head" in tables:
            reader.index = tables["head"][0]
            reader.take(50)
            loca_format = reader.i16()

        glyph_count = 0
        if "maxp" in tables:
            reader.index = tables["maxp"][0]
            reader.take(4)
            glyph_count = reader.u16()

        loca: list[int] = []
        if "loca" in tables:
            offset, length = tables["loca"]
            reader.index = offset
            width = 2 if loca_format == 0 else 4
            for _ in range(0, length, width):
                loca.append(reader.u16() * 2 if width == 2 else reader.u32())

        cmap_records: list[tuple[int, int, int]] = []
        cmap_tables: dict[int, tuple] = {}
        if "cmap" in tables:
            cmap_base = tables["cmap"][0]
            reader.index = cmap_base
            reader.u16()
            cmap_count = reader.u16()
            cmap_records = [(reader.u16(), reader.u16(), reader.u32())
                            for _ in range(cmap_count)]
            for _, _, relative in cmap_records:
                if relative in cmap_tables:
                    continue
                reader.index = cmap_base + relative
                table_start = reader.index
                fmt = reader.u16()
                if fmt == 0:
                    length, language = reader.u16(), reader.u16()
                    cmap_tables[relative] = (fmt, reader.take(length - 6), language)
                elif fmt == 4:
                    length, language = reader.u16(), reader.u16()
                    seg_count = reader.u16() // 2
                    reader.take(6)
                    ends = [reader.u16() for _ in range(seg_count)]
                    reader.u16()
                    starts = [reader.u16() for _ in range(seg_count)]
                    deltas = [reader.i16() for _ in range(seg_count)]
                    ranges = [reader.u16() for _ in range(seg_count)]
                    remaining = (table_start + length - reader.index) // 2
                    glyph_ids = [reader.u16() for _ in range(remaining)]
                    cmap_tables[relative] = (
                        fmt, ends, starts, deltas, ranges, glyph_ids, language)
                elif fmt == 6:
                    length, language = reader.u16(), reader.u16()
                    first, count = reader.u16(), reader.u16()
                    cmap_tables[relative] = (
                        fmt, first, [reader.u16() for _ in range(count)], language, length)
                elif fmt == 12:
                    reader.u16()
                    length, language, count = reader.u32(), reader.u32(), reader.u32()
                    groups = [(reader.u32(), reader.u32(), reader.u32()) for _ in range(count)]
                    cmap_tables[relative] = (fmt, groups, language, length)

        preferred = ((3, 10), (0, 4), (3, 1), (1, 0), (0, 3), (0, 1))
        selected = 0
        for pair in preferred:
            match = next((relative for platform, encoding, relative in cmap_records
                          if (platform, encoding) == pair), 0)
            if match > 0:
                selected = match
                break
        self._cmap = cmap_tables.get(selected)

        # QueryTTF intentionally appends only simple glyphs. It does not retain
        # placeholders for empty/composite glyphs, so preserve that indexing quirk.
        glyphs: list[str] = []
        if "glyf" in tables and loca:
            glyf_base = tables["glyf"][0]
            for glyph_index in range(glyph_count):
                reader.index = glyf_base + loca[glyph_index]
                contour_count = reader.i16()
                if contour_count <= 0:
                    continue
                reader.take(8)
                ends = [reader.u16() for _ in range(contour_count)]
                reader.take(reader.u16())
                flag_count = ends[-1] + 1
                flags: list[int] = []
                while len(flags) < flag_count:
                    flag = reader.u8()
                    flags.append(flag)
                    if flag & 0x08:
                        flags.extend([flag] * reader.u8())
                xs: list[int] = []
                for flag in flags:
                    same = 1 if flag & 0x10 else -1
                    xs.append(same * reader.u8() if flag & 0x02
                              else (0 if same == 1 else reader.i16()))
                ys: list[int] = []
                for flag in flags:
                    same = 1 if flag & 0x20 else -1
                    ys.append(same * reader.u8() if flag & 0x04
                              else (0 if same == 1 else reader.i16()))
                glyphs.append("".join(str(value) for value in xs + ys))

        self.code_to_glyph: dict[int, str] = {}
        self.glyph_to_code: dict[str, int] = {}
        self.limit_min, self.limit_max = 2**31 - 1, 0
        for code in list(range(0, 0x100)) + list(range(0x3400, 130000)):
            glyph_id = self._glyph_index(code)
            if glyph_id == 0 or glyph_id >= len(glyphs):
                continue
            signature = glyphs[glyph_id]
            self.limit_min = min(self.limit_min, code)
            self.limit_max = max(self.limit_max, code)
            self.code_to_glyph[code] = signature
            self.glyph_to_code.setdefault(signature, code)

    def _glyph_index(self, code: int) -> int:
        if code == 0 or self._cmap is None:
            return 0
        table = self._cmap
        if table[0] == 0:
            return table[1][code] if code < len(table[1]) else 0
        if table[0] == 4:
            _, ends, starts, deltas, ranges, glyph_ids, _ = table
            if code > ends[-1]:
                return 0
            start, end = 0, len(ends) - 1
            while start + 1 < end:
                middle = (start + end) // 2
                if ends[middle] <= code:
                    start = middle
                else:
                    end = middle
            if ends[start] < code:
                start += 1
            if code < starts[start]:
                return 0
            if ranges[start]:
                index = code - starts[start] + ranges[start] // 2 - (len(ranges) - start)
                return glyph_ids[index]
            return (code + deltas[start]) & 0xFFFF
        if table[0] == 6:
            index = code - table[1]
            return table[2][index] if 0 <= index < len(table[2]) else 0
        if table[0] == 12:
            groups = table[1]
            if not groups or code > groups[-1][1]:
                return 0
            for first, last, glyph in groups:
                if first <= code <= last:
                    return glyph + code - first
        return 0

    def get_name(self, name_id: int) -> str:
        table_offset, string_offset = self._name_header
        for platform, _encoding, _language, current_id, length, offset in self._names:
            if current_id == name_id:
                raw = self.raw[table_offset + string_offset + offset:
                               table_offset + string_offset + offset + length]
                return raw.decode("utf-8" if platform == 1 else "utf-16-be")
        return "error"

