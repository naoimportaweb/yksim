"""yksim — simulador de YubiKey 5 NFC (firmware 5.8.0) com PIV, para desenvolvimento e testes.

Usa uma CA de atestação de TESTE própria; a atestação NÃO valida como Yubico genuína.
"""

from .card import VirtualYubiKey
from .state import DeviceState

__all__ = ["VirtualYubiKey", "DeviceState", "open_sim"]


def open_sim(serial: int | None = None, state_path: str | None = None) -> VirtualYubiKey:
    """Cria (ou carrega) uma YubiKey virtual. Sem state_path, tudo fica só em memória.

    Sem serial, uma chave nova recebe um serial aleatório de 8 dígitos.
    """
    import os

    if state_path and os.path.exists(state_path):
        state = DeviceState.load(state_path)
    else:
        state = DeviceState(serial=serial) if serial else DeviceState()
    return VirtualYubiKey(state, state_path)
