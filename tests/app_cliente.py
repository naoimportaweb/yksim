"""App Qt de teste: um cliente que pede a YubiKey, como um aplicativo real faria (não é um teste do pytest).

    python3 tests/app_cliente.py

Fala com a chave só pelo PC/SC (pcscd), igual ao ykman e ao PRIS, então serve para conferir o yksim de ponta a
ponta: abra o simulador (`yubikey`), deixe-o ligado e use este app.

1. Procurar: lista as YubiKeys visíveis (serial e firmware).
2. Verificar PIN: confere o PIN (padrão 123456) e mostra as tentativas restantes.
3. Gerar chave no slot: ECC P-256 com PIN ALWAYS e toque ALWAYS (usa a chave de gerenciamento padrão).
4. Assinar desafio: assina 32 bytes aleatórios com o slot e confere a assinatura com a chave pública.
   Com toque ALWAYS, o simulador pede o toque: clique no disco dourado em até 15 s.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

# O vpcd cria dois leitores e o segundo fica sem cartão: o ykman registra essa falha com traceback. Silencia.
logging.getLogger("ykman").setLevel(logging.CRITICAL)

SLOTS = ("9c", "9a", "9d", "9e")


class Bridge(QObject):
    """Leva mensagens das threads de trabalho para a janela (Qt só pode ser mexido na thread principal)."""
    log = Signal(str, str)
    devices = Signal(list)
    done = Signal()


def _open(serial: int):
    """Abre uma conexão PC/SC com a chave de serial `serial`."""
    from ykman.device import list_all_devices
    from yubikit.core.smartcard import SmartCardConnection

    for dev, info in list_all_devices():
        if info.serial == serial:
            return dev.open_connection(SmartCardConnection)
    raise RuntimeError(f"a chave {serial} não está mais visível")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cliente de teste — pede a YubiKey")
        self.bridge = Bridge()
        self.bridge.log.connect(self._append)
        self.bridge.devices.connect(self._set_devices)
        self.bridge.done.connect(lambda: self._busy(False))

        root = QWidget()
        lay = QVBoxLayout(root)
        top = QHBoxLayout()
        self.find_btn = QPushButton("Procurar YubiKey")
        self.find_btn.clicked.connect(self.find)
        top.addWidget(self.find_btn)
        self.devices = QComboBox()
        self.devices.setMinimumWidth(280)
        top.addWidget(self.devices, 1)
        lay.addLayout(top)

        form = QFormLayout()
        self.pin = QLineEdit("123456")
        self.pin.setEchoMode(QLineEdit.Password)
        self.pin.setMaxLength(8)
        form.addRow("PIN:", self.pin)
        self.slot = QComboBox()
        self.slot.addItems(SLOTS)
        form.addRow("Slot:", self.slot)
        lay.addLayout(form)

        actions = QHBoxLayout()
        self.buttons = [self.find_btn]
        for text, fn in (("Verificar PIN", self.verify_pin), ("Gerar chave no slot", self.generate),
                         ("Assinar desafio", self.sign)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            actions.addWidget(b)
            self.buttons.append(b)
        lay.addLayout(actions)

        self.status = QLabel("")
        self.status.setStyleSheet("font-weight: bold; color: #2e7d32;")
        lay.addWidget(self.status)
        self.out = QPlainTextEdit(readOnly=True)
        self.out.setStyleSheet("font-family: monospace;")
        lay.addWidget(self.out, 1)
        self.setCentralWidget(root)
        self.resize(640, 440)
        self.find()

    # ---- infraestrutura -------------------------------------------------------
    def _append(self, msg: str, kind: str):
        color = {"err": "#c62828", "ok": "#2e7d32"}.get(kind, "#333")
        msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.out.appendHtml(f'<span style="color:{color}">{msg}</span>')

    def _busy(self, on: bool, text: str = ""):
        for b in self.buttons:
            b.setEnabled(not on)
        self.status.setText(text if on else "")

    def _run(self, text: str, fn, needs_key: bool = True):
        """Roda `fn(serial)` numa thread: operações com toque esperam até 15 s e a janela não pode travar."""
        serial = self.devices.currentData()
        if needs_key and serial is None:
            self.bridge.log.emit("Nenhuma YubiKey selecionada: clique em Procurar.", "err")
            return
        self._busy(True, text)

        def work():
            try:
                fn(serial)
            except Exception as e:  # noqa: BLE001 - o objetivo do app é mostrar o erro
                self.bridge.log.emit(f"Erro: {type(e).__name__}: {e}", "err")
                if "unpowered" in str(e).lower():
                    self.bridge.log.emit("O pcscd deixou o cartão sem energia. No simulador: DESLIGAR e LIGAR, "
                                         "depois Procurar de novo.", "err")
                logging.debug("%s", traceback.format_exc())
            finally:
                self.bridge.done.emit()
        threading.Thread(target=work, daemon=True).start()

    def _set_devices(self, devices: list):
        current = self.devices.currentData()
        self.devices.clear()
        for serial, label in devices:
            self.devices.addItem(label, serial)
        i = self.devices.findData(current)
        self.devices.setCurrentIndex(i if i >= 0 else 0)

    # ---- ações ---------------------------------------------------------------
    def find(self):
        def work(_):
            from smartcard.System import readers
            from ykman.device import list_all_devices

            rs = readers()
            self.bridge.log.emit(f"Leitores PC/SC: {', '.join(map(str, rs)) or 'nenhum'}", "")
            found = [(info.serial, f"{info.serial} · firmware {info.version}") for _, info in list_all_devices()]
            self.bridge.devices.emit(found)
            if found:
                for _, label in found:
                    self.bridge.log.emit(f"YubiKey encontrada: {label}", "ok")
            else:
                self.bridge.log.emit("Nenhuma YubiKey respondeu. O simulador está ligado? Se o leitor aparece mas a chave não, "
                                     "use DESLIGAR e LIGAR no simulador e procure de novo.", "err")
        self._run("Procurando...", work, needs_key=False)

    def verify_pin(self):
        pin = self.pin.text()

        def work(serial):
            from yubikit.piv import InvalidPinError, PivSession

            with _open(serial) as conn:
                piv = PivSession(conn)
                try:
                    piv.verify_pin(pin)
                    self.bridge.log.emit(f"PIN correto ({piv.get_pin_attempts()} tentativas)", "ok")
                except InvalidPinError as e:
                    self.bridge.log.emit(f"PIN incorreto: restam {e.attempts_remaining} tentativas", "err")
        self._run("Verificando PIN...", work)

    def generate(self):
        slot_hex = self.slot.currentText()

        def work(serial):
            from yubikit.piv import (DEFAULT_MANAGEMENT_KEY, KEY_TYPE, PIN_POLICY, SLOT, TOUCH_POLICY,
                                     PivSession)

            with _open(serial) as conn:
                piv = PivSession(conn)
                piv.authenticate(DEFAULT_MANAGEMENT_KEY)
                pub = piv.generate_key(SLOT(int(slot_hex, 16)), KEY_TYPE.ECCP256, PIN_POLICY.ALWAYS,
                                       TOUCH_POLICY.ALWAYS)
                self.bridge.log.emit(f"Chave ECCP256 gerada no slot {slot_hex} (PIN ALWAYS, toque ALWAYS): "
                                     f"{_fingerprint(pub)}", "ok")
        self._run(f"Gerando chave no slot {slot_hex}...", work)

    def sign(self):
        pin, slot_hex = self.pin.text(), self.slot.currentText()

        def work(serial):
            from yubikit.piv import SLOT, PivSession, TOUCH_POLICY

            slot = SLOT(int(slot_hex, 16))
            with _open(serial) as conn:
                piv = PivSession(conn)
                md = piv.get_slot_metadata(slot)
                challenge = os.urandom(32)
                piv.verify_pin(pin)
                if md.touch_policy in (TOUCH_POLICY.ALWAYS, TOUCH_POLICY.CACHED):
                    self.bridge.log.emit("TOQUE NA CHAVE (clique no disco dourado do simulador)...", "")
                pad = padding.PKCS1v15() if md.key_type.name.startswith("RSA") else None
                sig = piv.sign(slot, md.key_type, challenge, hashes.SHA256(), pad)
                pub = md.public_key
                if isinstance(pub, ec.EllipticCurvePublicKey):
                    pub.verify(sig, challenge, ec.ECDSA(hashes.SHA256()))
                elif isinstance(pub, rsa.RSAPublicKey):
                    pub.verify(sig, challenge, padding.PKCS1v15(), hashes.SHA256())
                else:
                    pub.verify(sig, challenge)
                self.bridge.log.emit(f"Desafio assinado pelo slot {slot_hex} ({md.key_type.name}) e assinatura "
                                     f"conferida com a chave pública {_fingerprint(pub)}", "ok")
        self._run(f"Assinando com o slot {slot_hex}... (toque na chave se pedido)", work)


def _fingerprint(pub) -> str:
    from cryptography.hazmat.primitives import serialization

    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashes.Hash(hashes.SHA256())
    h.update(der)
    return "SHA256:" + h.finalize().hex()[:16]


def main() -> int:
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.WARNING)
    app = QApplication(sys.argv[:1])
    w = Window()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
