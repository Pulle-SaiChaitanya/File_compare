"""cp1047 (EBCDIC Open Systems) is missing from Python's standard codecs; derive it from cp037."""
from __future__ import annotations

import codecs

# Code points where IBM CCSID 1047 differs from CCSID 37.
_OVERRIDES = {
    0x15: "\n", 0x25: "\x85", 0x5F: "^", 0xAD: "[", 0xB0: "\u00ac",
    0xBA: "\u00dd", 0xBB: "\u00a8", 0xBD: "]",
}


def _search(name: str):
    if name.lower().replace("_", "-") != "cp1047":
        return None
    table = list(bytes(range(256)).decode("cp037"))
    for byte, char in _OVERRIDES.items():
        table[byte] = char
    decoding = "".join(table)
    encoding = codecs.charmap_build(decoding)

    class Codec(codecs.Codec):
        def encode(self, text, errors="strict"):
            return codecs.charmap_encode(text, errors, encoding)

        def decode(self, data, errors="strict"):
            return codecs.charmap_decode(data, errors, decoding)

    class IncrementalDecoder(codecs.IncrementalDecoder):
        def decode(self, data, final=False):
            return codecs.charmap_decode(data, self.errors, decoding)[0]

    codec = Codec()
    return codecs.CodecInfo(codec.encode, codec.decode, name="cp1047",
                            incrementaldecoder=IncrementalDecoder)


def register() -> None:
    try:
        codecs.lookup("cp1047")
    except LookupError:
        codecs.register(_search)
