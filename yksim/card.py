"""Cartão virtual: parse de APDU (curta/estendida), SELECT, encadeamento e GET RESPONSE."""

from __future__ import annotations

import logging
from typing import Callable

from . import management, piv
from .errors import SW, ApduError
from .state import DeviceState

log = logging.getLogger("yksim.card")

# ATR de uma YubiKey 5 via USB (CCID). ATR[1] & 0xF0 == 0xF0 => ykman trata como USB.
ATR_USB = bytes.fromhex("3bfd1300008131fe158073c021c057597562694b657940")

INS_SELECT = 0xA4
INS_GET_RESPONSE = 0xC0
CHUNK = 256


def parse_apdu(apdu: bytes) -> tuple[int, int, int, int, bytes]:
    if len(apdu) < 4:
        raise ApduError(SW.WRONG_LENGTH)
    cla, ins, p1, p2 = apdu[:4]
    body = apdu[4:]
    if len(body) <= 1:  # caso 1 ou caso 2 curto
        return cla, ins, p1, p2, b""
    if body[0] != 0:  # curta
        lc = body[0]
        data = body[1 : 1 + lc]
        if len(data) != lc or len(body) not in (1 + lc, 2 + lc):
            raise ApduError(SW.WRONG_LENGTH)
        return cla, ins, p1, p2, data
    # estendida
    if len(body) == 3:  # caso 2 estendido (apenas Le)
        return cla, ins, p1, p2, b""
    lc = int.from_bytes(body[1:3], "big")
    data = body[3 : 3 + lc]
    if len(data) != lc or len(body) not in (3 + lc, 5 + lc):
        raise ApduError(SW.WRONG_LENGTH)
    return cla, ins, p1, p2, data


class VirtualYubiKey:
    """Uma YubiKey 5 NFC virtual com os applets de gerenciamento e PIV."""

    def __init__(
        self,
        state: DeviceState,
        state_path: str | None = None,
        touch: Callable[[int], bool] | None = None,
    ):
        self.state = state
        self.state_path = state_path
        save = lambda: state.save(state_path)  # noqa: E731
        self.applets = [
            (management.AID, management.ManagementApplet(state, save)),
            (piv.AID, piv.PivApplet(state, save, touch)),
        ]
        self.atr = ATR_USB
        # Gancho opcional chamado após cada APDU: on_transmit(apdu, resposta)
        self.on_transmit: Callable[[bytes, bytes], None] | None = None
        self.power_on()

    # ---- energia -------------------------------------------------------------
    def power_on(self) -> None:
        self.selected = None
        self._chain = b""
        self._pending = b""
        for _, applet in self.applets:
            applet.deselect()

    power_off = reset = power_on

    # ---- APDU ----------------------------------------------------------------
    def transmit(self, apdu: bytes) -> bytes:
        try:
            data, sw = self._transmit(apdu), SW.OK
        except ApduError as e:
            data, sw = b"", e.sw
            log.debug("APDU %s -> %04X %s", apdu[:4].hex(), sw, e)
        except Exception:  # nunca derrube o leitor por causa de um bug
            log.exception("Erro interno processando %s", apdu.hex())
            data, sw = b"", SW.UNKNOWN
        if sw == SW.OK and len(data) > CHUNK:
            self._pending = data[CHUNK:]
            data = data[:CHUNK]
            sw = 0x6100 | min(len(self._pending), 0xFF) if len(self._pending) < 256 else 0x6100
        resp = data + sw.to_bytes(2, "big")
        if self.on_transmit:
            try:
                self.on_transmit(apdu, resp)
            except Exception:
                log.exception("on_transmit falhou")
        return resp

    def _transmit(self, apdu: bytes) -> bytes:
        cla, ins, p1, p2, data = parse_apdu(apdu)

        if ins == INS_GET_RESPONSE and self._pending:
            out, self._pending = self._pending, b""
            return out
        self._pending = b""

        if cla & 0x10:  # encadeamento de comando
            self._chain += data
            return b""
        if self._chain:
            data, self._chain = self._chain + data, b""

        if ins == INS_SELECT and p1 == 0x04:
            return self._select(data)
        if self.selected is None:
            raise ApduError(SW.CONDITIONS_NOT_SATISFIED, "nenhum applet selecionado")
        return self.selected.process(cla & ~0x10, ins, p1, p2, data)

    def _select(self, aid: bytes) -> bytes:
        for full_aid, applet in self.applets:
            if aid and (full_aid.startswith(aid) or aid.startswith(full_aid)):
                if self.selected is not None and self.selected is not applet:
                    self.selected.deselect()
                self.selected = applet
                log.debug("SELECT %s", aid.hex())
                return applet.select()
        raise ApduError(SW.FILE_NOT_FOUND, f"AID {aid.hex()} não disponível")
