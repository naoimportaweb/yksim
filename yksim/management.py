"""Applet de gerenciamento (usado por `ykman info`, `ykman list`, `ykman config`)."""

from __future__ import annotations

from . import tlv
from .errors import SW, ApduError
from .state import DeviceState

AID = bytes.fromhex("a000000527471117")

INS_READ_CONFIG = 0x1D
INS_WRITE_CONFIG = 0x1C

TAG_USB_SUPPORTED = 0x01
TAG_SERIAL = 0x02
TAG_USB_ENABLED = 0x03
TAG_FORM_FACTOR = 0x04
TAG_VERSION = 0x05
TAG_AUTO_EJECT_TIMEOUT = 0x06
TAG_CHALRESP_TIMEOUT = 0x07
TAG_DEVICE_FLAGS = 0x08
TAG_CONFIG_LOCK = 0x0A
TAG_REBOOT = 0x0C
TAG_NFC_SUPPORTED = 0x0D
TAG_NFC_ENABLED = 0x0E
TAG_FIPS_CAPABLE = 0x14
TAG_FIPS_APPROVED = 0x15
TAG_PIN_COMPLEXITY = 0x16
TAG_NFC_RESTRICTED = 0x17
TAG_RESET_BLOCKED = 0x18


class ManagementApplet:
    def __init__(self, state: DeviceState, save):
        self.state = state
        self._save = save

    def select(self) -> bytes:
        v = ".".join(str(x) for x in self.state.version)
        return f"Virtual mgmt - FW version {v}".encode()

    def deselect(self) -> None:
        pass

    def process(self, cla: int, ins: int, p1: int, p2: int, data: bytes) -> bytes:
        if ins == INS_READ_CONFIG:
            return self._read_config(p1)
        if ins == INS_WRITE_CONFIG:
            return self._write_config(data)
        raise ApduError(SW.INS_NOT_SUPPORTED)

    def _read_config(self, page: int) -> bytes:
        if page != 0:
            return b"\x00"  # sem páginas adicionais
        s = self.state
        body = b"".join(
            [
                tlv.tlv(TAG_USB_SUPPORTED, s.usb_supported.to_bytes(2, "big")),
                tlv.tlv(TAG_SERIAL, s.serial.to_bytes(4, "big")),
                tlv.tlv(TAG_USB_ENABLED, s.usb_enabled.to_bytes(2, "big")),
                tlv.tlv(TAG_FORM_FACTOR, bytes([s.form_factor])),
                tlv.tlv(TAG_VERSION, bytes(s.version)),
                tlv.tlv(TAG_AUTO_EJECT_TIMEOUT, s.auto_eject_timeout.to_bytes(2, "big")),
                tlv.tlv(TAG_CHALRESP_TIMEOUT, bytes([s.chalresp_timeout])),
                tlv.tlv(TAG_DEVICE_FLAGS, bytes([s.device_flags])),
                tlv.tlv(TAG_CONFIG_LOCK, b"\x00"),
                tlv.tlv(TAG_NFC_SUPPORTED, s.nfc_supported.to_bytes(2, "big")),
                tlv.tlv(TAG_NFC_ENABLED, s.nfc_enabled.to_bytes(2, "big")),
                tlv.tlv(TAG_FIPS_CAPABLE, b"\x00\x00"),
                tlv.tlv(TAG_FIPS_APPROVED, b"\x00\x00"),
                tlv.tlv(TAG_PIN_COMPLEXITY, b"\x00"),
                tlv.tlv(TAG_NFC_RESTRICTED, b"\x00"),
                tlv.tlv(TAG_RESET_BLOCKED, b"\x00\x00"),
            ]
        )
        return bytes([len(body)]) + body

    def _write_config(self, data: bytes) -> bytes:
        if not data or data[0] != len(data) - 1:
            raise ApduError(SW.INCORRECT_DATA)
        s = self.state
        for tag, value in tlv.parse(data[1:]):
            n = int.from_bytes(value, "big") if value else 0
            if tag == TAG_USB_ENABLED:
                s.usb_enabled = n & s.usb_supported
            elif tag == TAG_NFC_ENABLED:
                s.nfc_enabled = n & s.nfc_supported
            elif tag == TAG_AUTO_EJECT_TIMEOUT:
                s.auto_eject_timeout = n
            elif tag == TAG_CHALRESP_TIMEOUT:
                s.chalresp_timeout = n
            elif tag == TAG_DEVICE_FLAGS:
                s.device_flags = n
            elif tag in (TAG_REBOOT, TAG_NFC_RESTRICTED):
                pass
            elif tag == TAG_CONFIG_LOCK:
                raise ApduError(SW.CONDITIONS_NOT_SATISFIED, "lock code não suportado")
        self._save()
        return b""
