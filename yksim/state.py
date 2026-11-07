"""Estado persistente do simulador (JSON em disco).

ATENÇÃO: as chaves privadas ficam gravadas SEM criptografia no arquivo de estado.
Isto é um simulador para desenvolvimento/testes, não um cofre de chaves.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field

# Serial de chave nova: sempre exatamente 8 dígitos, sorteado
SERIAL_MIN, SERIAL_MAX = 10_000_000, 99_999_999


def random_serial(exclude: set[int] | frozenset[int] = frozenset()) -> int:
    while True:
        n = SERIAL_MIN + secrets.randbelow(SERIAL_MAX - SERIAL_MIN + 1)
        if n not in exclude:
            return n


DEFAULT_PIN = b"123456"
DEFAULT_PUK = b"12345678"
DEFAULT_MGMT_KEY = bytes.fromhex("010203040506070801020304050607080102030405060708")
MGMT_AES192 = 0x0A  # padrão a partir do firmware 5.7


@dataclass
class SlotState:
    key_type: int
    pin_policy: int
    touch_policy: int
    origin: int  # 1 = gerada no dispositivo, 2 = importada
    private_pem: str


@dataclass
class DeviceState:
    serial: int = field(default_factory=random_serial)
    version: tuple[int, int, int] = (5, 8, 0)
    # Gerenciamento (ykman info / config)
    usb_supported: int = 0x033B  # OTP|U2F|OPENPGP|PIV|OATH|HSMAUTH|FIDO2
    usb_enabled: int = 0x033B
    nfc_supported: int = 0x033B
    nfc_enabled: int = 0x033B
    form_factor: int = 0x01  # USB-A keychain
    auto_eject_timeout: int = 0
    chalresp_timeout: int = 15
    device_flags: int = 0
    # PIV
    pin: str = DEFAULT_PIN.hex()
    puk: str = DEFAULT_PUK.hex()
    pin_total: int = 3
    pin_remaining: int = 3
    puk_total: int = 3
    puk_remaining: int = 3
    mgmt_key_type: int = MGMT_AES192
    mgmt_key: str = DEFAULT_MGMT_KEY.hex()
    mgmt_touch: int = 1  # TOUCH_POLICY.NEVER
    slots: dict[str, SlotState] = field(default_factory=dict)  # "9a" -> SlotState
    objects: dict[str, str] = field(default_factory=dict)  # "5fc105" -> hex
    # CA de atestação de TESTE (não é a CA da Yubico)
    attest_ca_key_pem: str = ""
    attest_ca_cert_pem: str = ""

    # ---- persistência -------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "DeviceState":
        with open(path) as f:
            raw = json.load(f)
        raw["version"] = tuple(raw["version"])
        raw["slots"] = {k: SlotState(**v) for k, v in raw.get("slots", {}).items()}
        return cls(**raw)

    def save(self, path: str | None) -> None:
        if not path:
            return
        data = asdict(self)
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".yksim-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ---- PIV reset ----------------------------------------------------
    def reset_piv(self) -> None:
        fresh = DeviceState()
        for name in (
            "pin",
            "puk",
            "pin_total",
            "pin_remaining",
            "puk_total",
            "puk_remaining",
            "mgmt_key_type",
            "mgmt_key",
            "mgmt_touch",
        ):
            setattr(self, name, getattr(fresh, name))
        # O reset apaga as chaves/objetos de usuário, mas preserva a atestação (F9)
        self.slots = {k: v for k, v in self.slots.items() if k == "f9"}
        self.objects = {k: v for k, v in self.objects.items() if k == "5fff01"}
