"""Validated reader for the observed 128-byte header / flagged length-descriptor format."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .work import Work

HEADER_SIZE = 128
HEADER_MAGIC = b"\x30\x00\x00\x7c"
DESCRIPTOR_FLAG = 0x40000000


@dataclass(frozen=True)
class VariableRecordRule:
    key_offset: int
    key_length: int
    base_length: int
    occurrence_length: int
    max_occurs: int


def _payloads(source: BinaryIO, length: int, work: Work,
              variable: VariableRecordRule | None = None,
              pad_records: bool = False) -> Iterator[bytes]:
    header = source.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE or header[:4] != HEADER_MAGIC:
        raise ValueError("Expected the supported 128-byte file header (magic 3000007C).")
    offset, count = HEADER_SIZE, 0
    while True:
        work.update("Validating headered record payloads", count)
        descriptor = source.read(4)
        if not descriptor:
            return
        if len(descriptor) != 4:
            raise ValueError(f"Truncated 4-byte record descriptor at offset {offset}.")
        value = int.from_bytes(descriptor, "big")
        if value >> 28 != 4:
            raise ValueError(f"Unsupported record descriptor flags at offset {offset}: {descriptor.hex().upper()}.")
        size = value & 0x0FFFFFFF
        if variable is None and size != length:
            raise ValueError(f"Record {count + 1} at offset {offset}: descriptor payload length {size} "
                             f"does not match schema length {length}.")
        if variable is not None and size > length:
            raise ValueError(f"Record {count + 1} at offset {offset}: payload length {size} exceeds "
                             f"the copybook maximum {length}.")
        payload = source.read(size)
        if len(payload) != size:
            raise ValueError(f"Truncated payload for record {count + 1} at offset {offset + 4}.")
        if variable is not None:
            key_end = variable.key_offset + variable.key_length
            if key_end > size:
                raise ValueError(f"Record {count + 1} at offset {offset}: payload length {size} does not "
                                 "contain the KEY-OCCURS field.")
            key = payload[variable.key_offset:key_end]
            if not key.isdigit():
                raise ValueError(f"Record {count + 1} at offset {offset}: KEY-OCCURS is not an "
                                 f"unsigned decimal value ({key.hex().upper()}).")
            occurs = int(key)
            if occurs > variable.max_occurs:
                raise ValueError(f"Record {count + 1} at offset {offset}: KEY-OCCURS {occurs} exceeds "
                                 f"the copybook maximum {variable.max_occurs}.")
            expected = variable.base_length + occurs * variable.occurrence_length
            if size != expected:
                raise ValueError(f"Record {count + 1} at offset {offset}: descriptor length {size} "
                                 f"does not match {variable.base_length} + KEY-OCCURS ({occurs}) * "
                                 f"{variable.occurrence_length} = {expected}.")
            if pad_records:
                payload = payload.ljust(length, b" ")
        next_offset = offset + 4 + size
        padding = (-next_offset) % 4
        if variable is None and padding:
            position = source.tell()
            immediate = source.read(4)
            source.seek(position)
            immediate_is_record = (len(immediate) == 4
                                   and int.from_bytes(immediate, "big") >> 28 == 4
                                   and (int.from_bytes(immediate, "big") & 0x0FFFFFFF) == length)
            if immediate_is_record:
                padding = 0
            else:
                pad = source.read(padding)
                following = source.read(4)
                following_is_record = (len(following) == 4
                                       and int.from_bytes(following, "big") >> 28 == 4
                                       and (int.from_bytes(following, "big") & 0x0FFFFFFF) == length)
                source.seek(position)
                if len(pad) == padding and following_is_record:
                    if any(pad):
                        raise ValueError(f"Nonzero alignment padding after record {count + 1} "
                                         f"at offset {next_offset}.")
                    padding = len(pad)
                elif len(pad) == padding and not following and not any(pad):
                    padding = len(pad)
                elif not pad and not following:
                    padding = 0
        if padding:
            pad = source.read(padding)
            if len(pad) != padding:
                raise ValueError(f"Truncated alignment padding after record {count + 1} at offset {next_offset}.")
            if any(pad):
                raise ValueError(f"Nonzero alignment padding after record {count + 1} at offset {next_offset}.")
        yield payload
        count += 1
        offset = next_offset + padding


def validate_headered_file(path: str, length: int, work: Work,
                           variable: VariableRecordRule | None = None) -> int:
    with open(path, "rb") as source:
        return sum(1 for _ in _payloads(source, length, work, variable))


def unpack_headered(data: bytes, length: int, work: Work,
                    variable: VariableRecordRule | None = None) -> bytes:
    return b"".join(_payloads(io.BytesIO(data), length, work, variable, pad_records=variable is not None))
