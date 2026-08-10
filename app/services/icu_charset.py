"""Small ctypes binding for ICU's charset detector (no Java runtime)."""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import re
import sys
from pathlib import Path


class _IcuCharsetDetector:
    def __init__(self):
        self.library, self.suffix = self._load_library()
        self.open = self._function("ucsdet_open")
        self.open.argtypes = [ctypes.POINTER(ctypes.c_int)]
        self.open.restype = ctypes.c_void_p
        self.set_text = self._function("ucsdet_setText")
        self.set_text.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                  ctypes.c_int32, ctypes.POINTER(ctypes.c_int)]
        self.detect = self._function("ucsdet_detect")
        self.detect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.detect.restype = ctypes.c_void_p
        self.get_name = self._function("ucsdet_getName")
        self.get_name.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.get_name.restype = ctypes.c_char_p
        self.get_confidence = self._function("ucsdet_getConfidence")
        self.get_confidence.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int)]
        self.get_confidence.restype = ctypes.c_int32
        self.detect_all = self._function("ucsdet_detectAll")
        self.detect_all.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int32),
                                    ctypes.POINTER(ctypes.c_int)]
        self.detect_all.restype = ctypes.POINTER(ctypes.c_void_p)
        self.close = self._function("ucsdet_close")
        self.close.argtypes = [ctypes.c_void_p]
        self.set_detectable = self._function("ucsdet_setDetectableCharset")
        self.set_detectable.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                        ctypes.c_int8, ctypes.POINTER(ctypes.c_int)]

    @staticmethod
    def _load_library():
        candidates = []
        library_dir = Path(sys.prefix) / "Library" / "bin"
        if library_dir.is_dir():
            candidates.extend(sorted(library_dir.glob("icuin[0-9]*.dll"),
                                     reverse=True))
            candidates.extend(library_dir.glob("icuin.dll"))
        located = ctypes.util.find_library("icui18n") or ctypes.util.find_library("icuin")
        if located:
            candidates.append(Path(located))
        for candidate in candidates:
            try:
                library = ctypes.WinDLL(str(candidate)) if sys.platform == "win32" \
                    else ctypes.CDLL(str(candidate))
            except OSError:
                continue
            match = re.search(r"(\d+)(?:\.dll)?$", candidate.name)
            return library, ("_" + match.group(1) if match else "")
        raise OSError("ICU charset detector library is unavailable")

    def _function(self, name):
        for exported in (name + self.suffix, name):
            try:
                return getattr(self.library, exported)
            except AttributeError:
                continue
        raise AttributeError(name)

    def name(self, raw: bytes) -> str | None:
        error = ctypes.c_int(0)
        detector = self.open(ctypes.byref(error))
        if not detector or error.value > 0:
            return None
        try:
            # Legado's vendored CharsetDetector registers these recognizers
            # but marks all four disabled by default. System ICU builds may
            # enable them, so enforce the fixed recognizer set explicitly.
            for charset in (b"IBM424_rtl", b"IBM424_ltr",
                            b"IBM420_rtl", b"IBM420_ltr"):
                self.set_detectable(detector, charset, 0, ctypes.byref(error))
                error.value = 0
            data = ctypes.create_string_buffer(raw)
            self.set_text(detector, data, len(raw), ctypes.byref(error))
            count = ctypes.c_int32(0)
            matches = self.detect_all(detector, ctypes.byref(count),
                                      ctypes.byref(error))
            if not matches or error.value > 0:
                return None
            disabled = {"IBM424_rtl", "IBM424_ltr",
                        "IBM420_rtl", "IBM420_ltr"}
            candidates = []
            for index in range(count.value):
                value = self.get_name(matches[index], ctypes.byref(error))
                if value and error.value <= 0:
                    name = value.decode("ascii")
                    if name not in disabled and not name.startswith("UTF-16") \
                            and not name.startswith("UTF-32") and name not in {
                                "Big5", "ISO-8859-7", "windows-1253"}:
                        confidence = self.get_confidence(
                            matches[index], ctypes.byref(error))
                        candidates.append((int(confidence),
                                           _recognizer_order(name), name))
            for name, confidence in _fixed_unicode_scores(raw).items():
                if confidence > 0:
                    candidates.append((confidence, _recognizer_order(name), name))
            big5 = _fixed_big5_confidence(raw)
            if big5:
                candidates.append((big5, _recognizer_order("Big5"), "Big5"))
            greek = _fixed_greek_confidence(raw)
            if greek:
                greek_name = ("windows-1253" if any(
                    0x80 <= value <= 0x9F for value in raw[:8000])
                              else "ISO-8859-7")
                candidates.append((greek, _recognizer_order("ISO-8859-7"),
                                   greek_name))
            return max(candidates)[2] if candidates else None
        finally:
            self.close(detector)


try:
    _DETECTOR = _IcuCharsetDetector()
except (OSError, AttributeError):
    _DETECTOR = None


def detect_charset(raw: bytes) -> str | None:
    return _DETECTOR.name(raw) if _DETECTOR is not None else None


_FIXED_ORDER = {
    name: index for index, name in enumerate((
        "UTF-8", "UTF-16BE", "UTF-16LE", "UTF-32BE", "UTF-32LE",
        "Shift_JIS", "ISO-2022-JP", "ISO-2022-CN", "ISO-2022-KR",
        "GB18030", "EUC-JP", "EUC-KR", "Big5", "ISO-8859-1",
        "ISO-8859-2", "ISO-8859-5", "ISO-8859-6", "ISO-8859-7",
        "ISO-8859-8-I", "ISO-8859-8", "windows-1251", "windows-1256",
        "KOI8-R", "ISO-8859-9"))}


def _recognizer_order(name: str) -> int:
    normalized = {"Shift_JIS": "Shift_JIS", "windows-1250": "ISO-8859-2",
                  "windows-1252": "ISO-8859-1"}.get(name, name)
    return _FIXED_ORDER.get(normalized, -1)


def _utf16_confidence(raw: bytes, little: bool) -> int:
    confidence = 10
    limit = min(len(raw), 30)
    for index in range(0, limit - 1, 2):
        first, second = raw[index:index + 2]
        code_unit = ((second << 8) | first) if little else ((first << 8) | second)
        if index == 0 and code_unit == 0xFEFF:
            return 100
        if code_unit == 0:
            confidence -= 10
        elif 0x20 <= code_unit <= 0xFF or code_unit == 0x0A:
            confidence += 10
        confidence = min(100, max(0, confidence))
        if confidence in (0, 100):
            break
    return 0 if limit < 4 and confidence < 100 else confidence


def _utf32_confidence(raw: bytes, little: bool) -> int:
    limit = len(raw) // 4 * 4
    if not limit:
        return 0
    byte_order = "little" if little else "big"
    values = [int.from_bytes(raw[index:index + 4], byte_order, signed=False)
              for index in range(0, limit, 4)]
    has_bom = values[0] == 0xFEFF
    valid = sum(value < 0x10FFFF and not 0xD800 <= value <= 0xDFFF
                for value in values)
    invalid = len(values) - valid
    if has_bom and invalid == 0:
        return 100
    if has_bom and valid > invalid * 10:
        return 80
    if valid > 3 and invalid == 0:
        return 100
    if valid > 0 and invalid == 0:
        return 80
    return 25 if valid > invalid * 10 else 0


def _fixed_unicode_scores(raw: bytes) -> dict[str, int]:
    return {
        "UTF-16BE": _utf16_confidence(raw, False),
        "UTF-16LE": _utf16_confidence(raw, True),
        "UTF-32BE": _utf32_confidence(raw, False),
        "UTF-32LE": _utf32_confidence(raw, True),
    }


_BIG5_COMMON = {
    0xa140, 0xa141, 0xa142, 0xa143, 0xa147, 0xa149, 0xa175, 0xa176,
    0xa440, 0xa446, 0xa447, 0xa448, 0xa451, 0xa454, 0xa457, 0xa464,
    0xa46a, 0xa46c, 0xa477, 0xa4a3, 0xa4a4, 0xa4a7, 0xa4c1, 0xa4ce,
    0xa4d1, 0xa4df, 0xa4e8, 0xa4fd, 0xa540, 0xa548, 0xa558, 0xa569,
    0xa5cd, 0xa5e7, 0xa657, 0xa661, 0xa662, 0xa668, 0xa670, 0xa6a8,
    0xa6b3, 0xa6b9, 0xa6d3, 0xa6db, 0xa6e6, 0xa6f2, 0xa740, 0xa751,
    0xa759, 0xa7da, 0xa8a3, 0xa8a5, 0xa8ad, 0xa8d1, 0xa8d3, 0xa8e4,
    0xa8fc, 0xa9c0, 0xa9d2, 0xa9f3, 0xaa6b, 0xaaba, 0xaabe, 0xaacc,
    0xaafc, 0xac47, 0xac4f, 0xacb0, 0xacd2, 0xad59, 0xaec9, 0xafe0,
    0xb0ea, 0xb16f, 0xb2b3, 0xb2c4, 0xb36f, 0xb44c, 0xb44e, 0xb54c,
    0xb5a5, 0xb5bd, 0xb5d0, 0xb5d8, 0xb671, 0xb7ed, 0xb867, 0xb944,
    0xbad8, 0xbb44, 0xbba1, 0xbdd1, 0xc2c4, 0xc3b9, 0xc440, 0xc45f,
}


def _fixed_big5_confidence(raw: bytes) -> int:
    index = total = double = common = bad = 0
    while index < len(raw):
        first = raw[index]
        index += 1
        if first <= 0x7F or first == 0xFF:
            total += 1
            continue
        if index >= len(raw):
            break
        second = raw[index]
        index += 1
        total += 1
        if second < 0x40 or second in (0x7F, 0xFF):
            bad += 1
        else:
            double += 1
            common += ((first << 8) | second) in _BIG5_COMMON
        if bad >= 2 and bad * 5 >= double:
            break
    if double <= 10 and bad == 0:
        return 0 if double == 0 and total < 10 else 10
    if double < 20 * bad:
        return 0
    scale = 90.0 / math.log(double / 4.0)
    return min(int(math.log(common + 1) * scale + 10), 100)


_GREEK_BYTE_MAP = [
    32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 32, 0, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111,
    112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 32, 32, 32, 32, 32,
    32, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111,
    112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 161, 162, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32, 32,
    32, 32, 32, 32, 32, 32, 220, 32, 221, 222, 223, 32, 252, 32, 253, 254,
    192, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239,
    240, 241, 32, 243, 244, 245, 246, 247, 248, 249, 250, 251, 220, 221, 222, 223,
    224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239,
    240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 32,
]

_GREEK_NGRAMS = {
    2154989, 2154992, 2155497, 2155753, 2156016, 2156320, 2157281,
    2157797, 2158049, 2158368, 2158817, 2158831, 2158833, 2159604,
    2159605, 2159847, 2159855, 14672160, 14754017, 14754036, 14805280,
    14806304, 14807292, 14807584, 14936545, 15067424, 15069728,
    15147252, 15199520, 15200800, 15278324, 15327520, 15330014,
    15331872, 15393257, 15393268, 15525152, 15540449, 15540453,
    15540464, 15589664, 15725088, 15725856, 15790069, 15790575,
    15793184, 15868129, 15868133, 15868138, 15868144, 15868148,
    15983904, 15984416, 15987951, 16048416, 16048617, 16050157,
    16050162, 16050666, 16052000, 16052213, 16054765, 16379168,
    16706848,
}


def _fixed_greek_confidence(raw: bytes) -> int:
    ngram = count = hits = 0
    ignore_space = False
    for value in raw[:8000]:
        mapped = _GREEK_BYTE_MAP[value]
        if mapped and not (mapped == 0x20 and ignore_space):
            ngram = ((ngram << 8) + mapped) & 0xFFFFFF
            count += 1
            hits += ngram in _GREEK_NGRAMS
        if mapped:
            ignore_space = mapped == 0x20
    ngram = ((ngram << 8) + 0x20) & 0xFFFFFF
    count += 1
    hits += ngram in _GREEK_NGRAMS
    raw_percent = hits / count
    return 98 if raw_percent > 0.33 else int(raw_percent * 300.0)
