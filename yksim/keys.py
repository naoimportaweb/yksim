"""Várias chaves virtuais num diretório: cada chave é um arquivo `<serial>.json`.

    ~/.local/share/yksim/39072602.json
    ~/.local/share/yksim/48120577.json

Só uma chave fica "plugada" por vez (há um único leitor vpcd). A chave usada por último é a de arquivo
modificado mais recentemente: abrir uma chave regrava o arquivo, então não há arquivo de índice.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable

from .card import VirtualYubiKey
from .state import SERIAL_MAX, SERIAL_MIN, DeviceState, random_serial

log = logging.getLogger("yksim.keys")

DEFAULT_DIR = os.path.expanduser("~/.local/share/yksim")
LEGACY_FILE = "state.json"  # formato antigo: uma chave só
_KEY_FILE = re.compile(r"^(\d+)\.json$")


def serial_arg(text: str) -> int:
    """Tipo para argparse: serial com exatamente 8 dígitos."""
    if not re.fullmatch(r"\d{8}", text) or not SERIAL_MIN <= int(text) <= SERIAL_MAX:
        import argparse

        raise argparse.ArgumentTypeError("o serial deve ter exatamente 8 dígitos (10000000–99999999)")
    return int(text)


class KeyStore:
    def __init__(self, directory: str = DEFAULT_DIR):
        self.dir = directory
        self._migrate_legacy()

    def path(self, serial: int) -> str:
        return os.path.join(self.dir, f"{serial}.json")

    def serials(self) -> list[int]:
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        return sorted(int(m.group(1)) for n in names if (m := _KEY_FILE.match(n)))

    def exists(self, serial: int) -> bool:
        return os.path.exists(self.path(serial))

    def last_used(self) -> int | None:
        serials = self.serials()
        return max(serials, key=lambda s: os.path.getmtime(self.path(s))) if serials else None

    def load(self, serial: int) -> DeviceState:
        return DeviceState.load(self.path(serial))

    def create(self, serial: int | None = None) -> int:
        """Cria uma chave nova (serial aleatório de 8 dígitos, se não for dado) e devolve o serial."""
        if serial is None:
            serial = random_serial(exclude=set(self.serials()))
        elif self.exists(serial):
            raise FileExistsError(f"já existe uma chave com o serial {serial}")
        state = DeviceState(serial=serial)
        VirtualYubiKey(state, self.path(serial))  # gera a CA de teste e a atestação F9, e grava
        state.save(self.path(serial))
        log.info("Chave nova criada: %d", serial)
        return serial

    def open(self, serial: int, touch: Callable[[int], bool] | None = None) -> VirtualYubiKey:
        """Abre uma chave existente e a marca como a usada por último."""
        path = self.path(serial)
        state = DeviceState.load(path)
        card = VirtualYubiKey(state, path, touch)
        state.save(path)
        return card

    def open_default(self, serial: int | None = None, touch: Callable[[int], bool] | None = None) -> VirtualYubiKey:
        """Abre `serial` (criando-a se não existir) ou, sem serial, a última usada (ou uma nova)."""
        if serial is None:
            serial = self.last_used()
        if serial is None or not self.exists(serial):
            serial = self.create(serial)
        return self.open(serial, touch)

    def _migrate_legacy(self) -> None:
        """Renomeia o antigo `state.json` para `<serial>.json`."""
        old = os.path.join(self.dir, LEGACY_FILE)
        if not os.path.exists(old):
            return
        try:
            serial = DeviceState.load(old).serial
        except Exception:
            log.exception("Não consegui ler %s; deixando como está", old)
            return
        new = self.path(serial)
        if os.path.exists(new):
            log.warning("%s não migrado: %s já existe", old, new)
            return
        os.replace(old, new)
        log.info("Estado antigo migrado: %s -> %s", old, new)
