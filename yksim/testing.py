"""Conexão in-process para usar o simulador direto com `yubikit` (sem pcscd/vpcd).

    from yksim import open_sim
    from yksim.testing import SimConnection
    from yubikit.piv import PivSession

    piv = PivSession(SimConnection(open_sim()))
"""

from __future__ import annotations

from yubikit.core import TRANSPORT
from yubikit.core.smartcard import SmartCardConnection

from .card import VirtualYubiKey


class SimConnection(SmartCardConnection):
    def __init__(self, card: VirtualYubiKey):
        self.card = card
        card.power_on()

    @property
    def transport(self) -> TRANSPORT:
        return TRANSPORT.USB

    def send_and_receive(self, apdu: bytes) -> tuple[bytes, int]:
        resp = self.card.transmit(bytes(apdu))
        return resp[:-2], int.from_bytes(resp[-2:], "big")

    def close(self) -> None:
        pass
