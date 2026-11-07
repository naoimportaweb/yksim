"""Estado de "hardware" da chave virtual: energia, toque, LED e log de atividade.

Independente de interface: usado pelas interfaces Qt (qt.py) e web (web.py). Com um KeyStore, troca a chave
no leitor entre as várias chaves do diretório.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .card import VirtualYubiKey
from .keys import KeyStore
from .names import INS_NAMES, KEY_TYPE_NAMES, MGMT_NAMES, PIN_POLICY_NAMES, TOUCH_POLICY_NAMES
from .state import DEFAULT_MGMT_KEY, DEFAULT_PIN, DEFAULT_PUK
from .vpcd import VpcdClient

TOUCH_TIMEOUT = 15.0
SETUP_SCRIPT = Path(__file__).resolve().parent.parent / "setup.sh"


class Controller:
    def __init__(self, card: VirtualYubiKey, host: str = "127.0.0.1", port: int = 35963,
                 store: KeyStore | None = None):
        self.host, self.port = host, port
        self.store = store
        self._cond = threading.Condition()
        self._switch_lock = threading.Lock()
        self.switching = False
        self.setup_running = False
        self.version = 0
        self.powered = False
        self.status = "disconnected"
        self.auto_touch = False
        self.apdu_count = 0
        self._touch_req: dict | None = None
        self._log: deque[dict] = deque(maxlen=400)
        self._log_id = 0

        self._attach(card)
        self._keys = store.serials() if store else [card.state.serial]
        self.client = VpcdClient(card, host, port, on_status=self._on_status)

    def _attach(self, card: VirtualYubiKey) -> None:
        self.card = card
        card.on_transmit = self._on_apdu
        card.applets[1][1]._touch_cb = self._touch_callback

    # ------------------------------------------------------------------ várias chaves
    def select_key(self, serial: int) -> None:
        """Troca a chave no leitor: desconecta a atual e conecta a outra (como trocar a chave física).

        Bloqueia enquanto o vpcd desconecta; chame fora da thread da interface.
        """
        if self.store is None:
            return
        with self._switch_lock:
            if serial == self.card.state.serial:
                return
            self.switching = True
            self._changed()
            try:
                was_on = self.powered
                self.set_power(False)
                card = self.store.open(serial)
                self._attach(card)
                self.client.card = card
                self.apdu_count = 0
                self._keys = self.store.serials()
                self.log(f"Chave {serial} selecionada", "evt")
                if was_on:
                    self.set_power(True)
            finally:
                self.switching = False
                self._changed()

    def reset_attempts(self) -> None:
        """Volta os contadores de PIN e PUK ao máximo (desbloqueia), sem mudar os valores.

        Atalho do simulador: numa chave física só o PUK desbloqueia o PIN, e nada desbloqueia o PUK.
        """
        s = self.card.state
        s.pin_remaining, s.puk_remaining = s.pin_total, s.puk_total
        s.save(self.card.state_path)
        self.log(f"Tentativas restauradas: PIN {s.pin_total}/{s.pin_total}, PUK {s.puk_total}/{s.puk_total}", "evt")

    # ------------------------------------------------------------------ setup.sh
    @staticmethod
    def _sudo_ok() -> bool:
        try:
            return subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run_setup(self, script: Path = SETUP_SCRIPT) -> int | None:
        """Roda o setup.sh (pcscd + vpcd + venv) e manda a saída para o log. Bloqueia; chame em thread.

        Sem terminal o sudo não tem onde pedir senha: se ele exigir senha, avisa e não roda.
        Devolve o código de saída (None se não rodou).
        """
        if self.setup_running:
            return None
        if not script.is_file():
            self.log(f"setup.sh não encontrado em {script}", "err")
            return None
        if not self._sudo_ok():
            self.log(f"O sudo pede senha: rode no terminal: cd {script.parent} && ./{script.name}", "err")
            return None
        self.setup_running = True
        self.log(f"Rodando {script}...", "evt")
        try:
            proc = subprocess.Popen(["bash", str(script)], cwd=script.parent, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(line, "err" if line.startswith("!!") else "")
            rc = proc.wait()
        except OSError as e:
            self.log(f"Falha ao rodar setup.sh: {e}", "err")
            return None
        finally:
            self.setup_running = False
            self._changed()
        if rc == 0:
            self.log("setup.sh concluído", "evt")
        else:
            self.log(f"setup.sh falhou (código {rc})", "err")
        return rc

    def new_key(self) -> int:
        """Cria uma chave com serial aleatório de 8 dígitos e já a seleciona."""
        if self.store is None:
            raise RuntimeError("sem diretório de chaves")
        serial = self.store.create()
        self.log(f"Chave nova criada: serial {serial}", "evt")
        self.select_key(serial)
        return serial

    # ------------------------------------------------------------------ notificação
    def _changed(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait_change(self, version: int, timeout: float = 15.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.version != version, timeout)
            return self.version

    def log(self, msg: str, kind: str = "") -> None:
        with self._cond:
            self._log_id += 1
            self._log.append({"id": self._log_id, "t": time.strftime("%H:%M:%S"), "msg": msg, "kind": kind})
        self._changed()

    # ------------------------------------------------------------------ energia
    def set_power(self, on: bool) -> None:
        if on == self.powered:
            return
        if on:
            self.powered = True
            self.card.power_on()
            self.client.start()
            self.log("Chave ligada (plugada)", "evt")
        else:
            self.powered = False
            self._cancel_touch()
            self.client.stop()
            self.log("Chave desligada (desplugada)", "evt")

    def _on_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        if status == "connected":
            self.log(f"Conectada ao pcscd via vpcd ({self.host}:{self.port})", "evt")
        else:
            self._changed()

    # ------------------------------------------------------------------ toque
    def _touch_callback(self, slot: int) -> bool:
        if self.auto_touch:
            self.log(f"Toque aprovado automaticamente (slot {slot:02X})", "evt")
            return True
        req = {"slot": slot, "event": threading.Event(), "ok": False,
               "deadline": time.monotonic() + TOUCH_TIMEOUT}
        self._touch_req = req
        self.log(f"Aguardando toque (slot {slot:02X})...", "evt")
        req["event"].wait(TOUCH_TIMEOUT)
        self._touch_req = None
        if req["ok"]:
            self.log("Toque confirmado", "evt")
        else:
            self.log("Toque não recebido (tempo esgotado/cancelado)", "err")
        return req["ok"]

    def _cancel_touch(self) -> None:
        req = self._touch_req
        if req:
            req["event"].set()

    def touch(self) -> None:
        if not self.powered:
            self.log("Chave desligada — toque ignorado", "err")
            return
        req = self._touch_req
        if req:
            req["ok"] = True
            req["event"].set()
        else:
            self.log("Toque (nenhuma operação aguardando; OTP não é emulado)")

    def set_auto_touch(self, value: bool) -> None:
        self.auto_touch = bool(value)
        self._changed()

    # ------------------------------------------------------------------ APDUs
    def _on_apdu(self, apdu: bytes, resp: bytes) -> None:
        self.apdu_count += 1
        if len(apdu) < 4 or apdu[0] & 0x10:
            self._changed()
            return
        ins = apdu[1]
        name = INS_NAMES.get(ins, f"INS {ins:02X}")
        if ins == 0xA4 and len(apdu) > 5:
            aid = apdu[5:5 + apdu[4]].hex()
            if aid.startswith("a000000308"):
                name += " PIV"
            elif aid.startswith("a000000527471117"):
                name += " Management"
            else:
                name += f" {aid}"
        sw = int.from_bytes(resp[-2:], "big")
        ok = sw == 0x9000 or sw >> 8 == 0x61
        self.log(f"{name:<24} → {sw:04X}", "" if ok else "err")

    # ------------------------------------------------------------------ snapshot
    def snapshot(self, since_log: int = 0) -> dict:
        s = self.card.state
        req = self._touch_req

        def tries(which, default):
            raw = bytes.fromhex(getattr(s, which))
            return {
                "value": raw.decode("utf-8", errors="backslashreplace"),
                "remaining": getattr(s, f"{which}_remaining"),
                "total": getattr(s, f"{which}_total"),
                "default": bytes.fromhex(getattr(s, which)) == default,
            }

        slots = []
        for k in sorted(s.slots):
            if k == "f9":
                continue
            st = s.slots[k]
            slots.append({
                "slot": k.upper(),
                "type": KEY_TYPE_NAMES.get(st.key_type, hex(st.key_type)),
                "origin": "gerada" if st.origin == 1 else "importada",
                "policy": PIN_POLICY_NAMES.get(st.pin_policy, "") + TOUCH_POLICY_NAMES.get(st.touch_policy, ""),
            })
        with self._cond:
            logs = [e for e in self._log if e["id"] > since_log]
        return {
            "version": self.version,
            "powered": self.powered,
            "status": self.status if self.powered else "disconnected",
            "auto_touch": self.auto_touch,
            "apdu_count": self.apdu_count,
            "touch": None if not req else {
                "slot": f"{req['slot']:02X}",
                "remaining": max(0.0, req["deadline"] - time.monotonic()),
            },
            "serial": s.serial,
            "keys": list(self._keys),
            "switching": self.switching,
            "setup_running": self.setup_running,
            "multi": self.store is not None,
            "firmware": ".".join(map(str, s.version)),
            "interfaces": "OTP+FIDO+CCID",
            "vpcd": f"{self.host}:{self.port}",
            "pin": tries("pin", DEFAULT_PIN),
            "puk": tries("puk", DEFAULT_PUK),
            "mgmt": {
                "type": MGMT_NAMES.get(s.mgmt_key_type, hex(s.mgmt_key_type)),
                "default": bytes.fromhex(s.mgmt_key) == DEFAULT_MGMT_KEY,
                "touch": s.mgmt_touch == 2,
            },
            "slots": slots,
            "log": logs,
        }

    def shutdown(self) -> None:
        self._cancel_touch()
        if self.client.running:
            self.client.stop()
