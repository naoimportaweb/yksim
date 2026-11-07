"""Cliente do protocolo vpcd (vsmartcard): conecta o cartão virtual ao pcscd.

Protocolo: cada mensagem = tamanho (uint16 big-endian) + payload.
Payload de 1 byte vindo do vpcd é um comando de controle:
  0x00 = desligar, 0x01 = ligar, 0x02 = reset, 0x04 = pedir ATR.
Qualquer outro payload é uma C-APDU, respondida com a R-APDU.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from .card import VirtualYubiKey

log = logging.getLogger("yksim.vpcd")

CTRL_OFF, CTRL_ON, CTRL_RESET, CTRL_ATR = 0, 1, 2, 4


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("vpcd fechou a conexão")
        buf += chunk
    return buf


def _send(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack(">H", len(payload)) + payload)


def serve(card: VirtualYubiKey, host: str = "127.0.0.1", port: int = 35963) -> None:
    """Conecta ao vpcd e atende APDUs para sempre (reconecta se cair)."""
    while True:
        try:
            with socket.create_connection((host, port)) as sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log.info("Conectado ao vpcd em %s:%d — cartão inserido", host, port)
                _loop(sock, card)
        except (ConnectionRefusedError, ConnectionError, OSError) as e:
            log.warning("vpcd indisponível (%s); tentando de novo em 2s...", e)
            time.sleep(2)


class VpcdClient:
    """Conexão com o vpcd que pode ser ligada/desligada (= plugar/desplugar a chave).

    on_status(str) recebe: "connecting", "connected", "disconnected".
    """

    def __init__(self, card: VirtualYubiKey, host="127.0.0.1", port=35963, on_status=None):
        self.card, self.host, self.port = card, host, port
        self.on_status = on_status or (lambda s: None)
        self._running = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="vpcd", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self.card.power_off()
        self.on_status("disconnected")

    def _run(self) -> None:
        while self._running:
            self.on_status("connecting")
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2)
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock
                if not self._running:
                    break
                log.info("Conectado ao vpcd em %s:%d — chave plugada", self.host, self.port)
                self.on_status("connected")
                _loop(sock, self.card)
            except (ConnectionError, OSError) as e:
                if self._running:
                    log.warning("vpcd indisponível (%s); tentando de novo...", e)
            finally:
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None
            for _ in range(10):  # espera 1 s antes de reconectar, saindo rápido se desligado
                if not self._running:
                    return
                time.sleep(0.1)


def _loop(sock: socket.socket, card: VirtualYubiKey) -> None:
    while True:
        (length,) = struct.unpack(">H", _recv_exact(sock, 2))
        msg = _recv_exact(sock, length)
        if length == 1:
            ctrl = msg[0]
            log.debug("ctrl %s", {CTRL_OFF: "OFF", CTRL_ON: "ON", CTRL_RESET: "RESET", CTRL_ATR: "ATR"}.get(ctrl, ctrl))
            if ctrl == CTRL_ATR:
                _send(sock, card.atr)
            elif ctrl == CTRL_ON:
                card.power_on()
            elif ctrl == CTRL_OFF:
                card.power_off()
            elif ctrl == CTRL_RESET:
                card.reset()
            else:
                log.warning("Comando de controle desconhecido: %r", ctrl)
            continue
        log.debug(">> %s", msg.hex())
        resp = card.transmit(msg)
        log.debug("<< %s", resp.hex())
        _send(sock, resp)
