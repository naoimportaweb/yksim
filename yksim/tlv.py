"""BER-TLV mínimo (mesmo formato usado pelo PIV / YubiKey)."""

from __future__ import annotations


def _encode_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n <= 0xFF:
        return bytes([0x81, n])
    if n <= 0xFFFF:
        return bytes([0x82]) + n.to_bytes(2, "big")
    raise ValueError("TLV muito longo")


def tlv(tag: int, value: bytes = b"") -> bytes:
    tag_bytes = tag.to_bytes((tag.bit_length() + 7) // 8 or 1, "big")
    return tag_bytes + _encode_len(len(value)) + value


def parse(data: bytes) -> list[tuple[int, bytes]]:
    """Retorna lista de (tag, valor) no nível superior."""
    out = []
    i = 0
    while i < len(data):
        tag = data[i]
        i += 1
        if tag & 0x1F == 0x1F:  # tag multi-byte
            while True:
                b = data[i]
                i += 1
                tag = (tag << 8) | b
                if not b & 0x80:
                    break
        ln = data[i]
        i += 1
        if ln == 0x81:
            ln = data[i]
            i += 1
        elif ln == 0x82:
            ln = int.from_bytes(data[i : i + 2], "big")
            i += 2
        elif ln > 0x82:
            raise ValueError("Comprimento TLV não suportado")
        if i + ln > len(data):
            raise ValueError("TLV truncado")
        out.append((tag, data[i : i + ln]))
        i += ln
    return out


def parse_dict(data: bytes) -> dict[int, bytes]:
    return dict(parse(data))
