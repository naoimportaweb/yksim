"""Interface nativa (PySide6/Qt) do yksim: janela nativa, sem navegador e sem porta HTTP.

    python -m yksim.qt                  # abre a última chave usada (ou cria uma)
    python -m yksim.qt --serial 39072602

- LIGAR/DESLIGAR (Ctrl+P): pluga/desconecta a chave do leitor virtual (vpcd/pcscd).
- Disco dourado: clique (ou Espaço) = tocar na chave. Quando uma operação exige toque, o LED pisca rápido e a
  contagem de 15 s aparece; sem toque no prazo, a operação falha, como na chave física.
- Chave: escolhe qual das chaves de ~/.local/share/yksim está no leitor; "Nova chave" cria uma com serial
  aleatório de 8 dígitos.
- LED: apagado = desligada; âmbar piscando = esperando o pcscd/vpcd; verde = conectada (pisca a cada APDU);
  verde piscando rápido = aguardando toque.

Usa o mesmo Controller da interface web (controller.py): a lógica da chave é uma só. Diferente da web, não abre
porta nenhuma - nada no computador (nem um site aberto no navegador) consegue ligar, desligar ou "tocar" a chave.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .controller import Controller
from .keys import DEFAULT_DIR, KeyStore, serial_arg

BG, PANEL, FG, MUTED = "#1e2227", "#262b31", "#e6e6e6", "#9aa3ad"
BODY, BODY_EDGE, METAL, METAL_DARK = "#141414", "#3a3a3a", "#c9ccd1", "#8d9198"
GOLD, GOLD_PRESSED, GOLD_EDGE = "#d4a93c", "#a8832a", "#8a6a1f"
LED_OFF, LED_GREEN, LED_AMBER, RED = "#3b3221", "#5dff7a", "#ffb347", "#ff6b6b"


class KeyWidget(QWidget):
    """A chave desenhada: corpo, conector USB e o disco de toque com o anel do LED."""
    touched = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedSize(240, 480)
        self.setCursor(Qt.PointingHandCursor)
        self.led = LED_OFF
        self.pressed = False
        self.plugged = 0.0          # 0 = desplugada (desce), 1 = plugada

    def _disc_center(self) -> QPointF:
        return QPointF(120, self._y0() + 330)

    def _y0(self) -> float:
        return 34 + 66 * (1 - self.plugged)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(PANEL))
        p.fillRect(QRectF(0, 0, 240, 34), QColor("#3d434b"))
        p.setPen(QPen(QColor("#5a6068"), 1))
        p.setBrush(QColor("#0c0c0c"))
        p.drawRect(QRectF(80, 22, 80, 12))
        p.setPen(QColor(MUTED))
        p.setFont(QFont("DejaVu Sans", 7))
        p.drawText(QRectF(0, 2, 240, 16), Qt.AlignCenter, "porta USB")
        y0 = self._y0()
        p.setPen(QPen(QColor(METAL_DARK), 2))
        p.setBrush(QColor(METAL))
        p.drawRect(QRectF(84, y0, 72, 72))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(METAL_DARK))
        for dx in (-16, 4):
            p.drawRect(QRectF(120 + dx, y0 + 18, 12, 10))
        p.setPen(QPen(QColor(BODY_EDGE), 2))
        p.setBrush(QColor(BODY))
        p.drawRoundedRect(QRectF(60, y0 + 70, 120, 306), 22, 22)
        p.setPen(QColor("#5f6670"))
        p.setFont(QFont("DejaVu Sans", 13, QFont.Bold))
        p.drawText(QRectF(60, y0 + 98, 120, 24), Qt.AlignCenter, "yksim")
        p.setPen(QColor("#4d535b"))
        p.setFont(QFont("DejaVu Sans", 8))
        p.drawText(QRectF(60, y0 + 124, 120, 18), Qt.AlignCenter, "5 NFC · virtual")
        c = self._disc_center()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.led))
        p.drawEllipse(c, 43, 43)
        p.setPen(QPen(QColor(GOLD_EDGE), 2))
        p.setBrush(QColor(GOLD_PRESSED if self.pressed else GOLD))
        p.drawEllipse(c, 36, 36)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 22, 22)
        p.setBrush(QColor(GOLD_EDGE))
        p.drawEllipse(c, 6, 6)

    def _on_disc(self, pos) -> bool:
        d = pos - self._disc_center()
        return d.x() ** 2 + d.y() ** 2 <= 43 ** 2

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._on_disc(e.position()):
            self.pressed = True
            self.update()

    def mouseReleaseEvent(self, e):
        if self.pressed:
            self.pressed = False
            self.update()
            if self._on_disc(e.position()):
                self.touched.emit()


class Window(QMainWindow):
    def __init__(self, ctrl: Controller, start_on: bool):
        super().__init__()
        self.ctrl = ctrl
        self.setWindowTitle(f"YubiKey virtual — {ctrl.card.state.serial}")
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {PANEL}; color: {FG}; font-family: 'DejaVu Sans'; font-size: 10pt; }}
            QLabel#muted {{ color: {MUTED}; }}
            QLabel#title {{ font-size: 13pt; font-weight: bold; }}
            QLabel#touch {{ color: {LED_GREEN}; font-size: 11pt; font-weight: bold; }}
            QPushButton {{ font-size: 12pt; font-weight: bold; padding: 10px 14px; border-radius: 6px; }}
            QPushButton#on {{ background: #2f7d32; color: white; }}
            QPushButton#off {{ background: #a33a3a; color: white; }}
            QPushButton#small {{ font-size: 10pt; padding: 4px 10px; background: #3a4048; }}
            QPushButton#small:disabled, QComboBox:disabled {{ color: {MUTED}; }}
            QComboBox {{ background: {BG}; border: 1px solid #3a4048; padding: 3px 8px; font-family: monospace; }}
            QPlainTextEdit, QTableWidget {{ background: {BG}; border: 1px solid #3a4048; font-family: monospace; }}
            QHeaderView::section {{ background: {BG}; color: {MUTED}; border: 0; padding: 3px; }}
        """)
        root = QWidget()
        lay = QHBoxLayout(root)
        left = QVBoxLayout()
        self.key = KeyWidget()
        self.key.touched.connect(self.touch)
        left.addWidget(self.key, alignment=Qt.AlignHCenter)
        self.power = QPushButton("LIGAR")
        self.power.clicked.connect(self.toggle_power)
        left.addWidget(self.power)
        hint = QLabel("Clique no disco dourado (ou Espaço) para tocar.\nCtrl+P liga/desliga.")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        left.addWidget(hint)
        self.auto = QCheckBox("Tocar automaticamente")
        self.auto.toggled.connect(ctrl.set_auto_touch)
        left.addWidget(self.auto)
        self.setup_btn = QPushButton("Configurar sistema (setup.sh)")
        self.setup_btn.setObjectName("small")
        self.setup_btn.setToolTip("Instala pcscd/vpcd, renomeia o leitor, mantém o pcscd ativo e cria a .venv (usa sudo)")
        self.setup_btn.clicked.connect(self._run_setup)
        left.addWidget(self.setup_btn)
        left.addStretch()
        lay.addLayout(left)

        right = QVBoxLayout()
        t = QLabel("YubiKey 5 NFC (virtual)")
        t.setObjectName("title")
        right.addWidget(t)
        keyrow = QHBoxLayout()
        lbl = QLabel("Chave:")
        lbl.setObjectName("muted")
        keyrow.addWidget(lbl)
        self.keys = QComboBox()
        self.keys.setToolTip("Qual chave virtual está plugada no leitor")
        self.keys.activated.connect(self._pick_key)
        keyrow.addWidget(self.keys, 1)
        self.new_btn = QPushButton("Nova chave")
        self.new_btn.setObjectName("small")
        self.new_btn.setToolTip("Cria uma chave nova com serial aleatório de 8 dígitos")
        self.new_btn.clicked.connect(self._new_key)
        keyrow.addWidget(self.new_btn)
        right.addLayout(keyrow)
        self._keys_shown: tuple = ()
        form = QFormLayout()
        self.info = {}
        for k, label in (("serial", "Serial"), ("firmware", "Firmware"), ("status", "Conexão"), ("pin", "PIN"),
                         ("puk", "PUK"), ("mgmt", "Gerenciamento"), ("apdus", "APDUs")):
            v = QLabel("—")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setTextFormat(Qt.RichText)
            self.info[k] = v
            lbl = QLabel(label + ":")
            lbl.setObjectName("muted")
            form.addRow(lbl, v)
        right.addLayout(form)
        self.reset_btn = QPushButton("Restaurar tentativas de PIN/PUK")
        self.reset_btn.setObjectName("small")
        self.reset_btn.setToolTip("Volta os contadores ao máximo e desbloqueia, sem mudar o PIN nem o PUK")
        self.reset_btn.clicked.connect(self.ctrl.reset_attempts)
        right.addWidget(self.reset_btn, alignment=Qt.AlignLeft)
        self.touch_lbl = QLabel("")
        self.touch_lbl.setObjectName("touch")
        right.addWidget(self.touch_lbl)
        self.slots = QTableWidget(0, 4)
        self.slots.setHorizontalHeaderLabels(["Slot", "Tipo", "Origem", "Políticas"])
        self.slots.verticalHeader().setVisible(False)
        self.slots.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.slots.setEditTriggers(QTableWidget.NoEditTriggers)
        self.slots.setMaximumHeight(150)
        right.addWidget(self.slots)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(2000)
        right.addWidget(self.log, 1)
        lay.addLayout(right, 1)
        self.setCentralWidget(root)
        self.resize(820, 520)

        for seq, fn in ((QKeySequence("Space"), self.touch), (QKeySequence("Ctrl+P"), self.toggle_power)):
            a = QAction(self)
            a.setShortcut(seq)
            a.triggered.connect(fn)
            self.addAction(a)

        self._version, self._last_log, self._apdus, self._blink = -1, 0, 0, 0
        self._activity_until = 0.0
        self._timer = QTimer(self, timeout=self._tick, interval=100)
        self._timer.start()
        if start_on:
            QTimer.singleShot(200, lambda: self.set_power(True))

    # ---- ações (o vpcd conecta numa thread: a janela nunca congela)
    def set_power(self, on: bool):
        threading.Thread(target=self.ctrl.set_power, args=(on,), daemon=True).start()

    def toggle_power(self):
        self.set_power(not self.ctrl.powered)

    def touch(self):
        self.ctrl.touch()

    def _pick_key(self, index: int):
        serial = self.keys.itemData(index)
        if serial is not None and serial != self.ctrl.card.state.serial:
            threading.Thread(target=self.ctrl.select_key, args=(serial,), daemon=True).start()

    def _run_setup(self):
        ok = QMessageBox.question(
            self, "Configurar sistema",
            "Rodar o setup.sh?\n\nEle usa sudo para instalar pcscd, vsmartcard-vpcd e opensc, renomear o leitor "
            "virtual e reiniciar o pcscd, e cria a .venv do projeto. A saída aparece no log.")
        if ok == QMessageBox.Yes:
            threading.Thread(target=self.ctrl.run_setup, daemon=True).start()

    def _new_key(self):
        def run():
            try:
                self.ctrl.new_key()
            except Exception as e:  # noqa: BLE001 - mostra no log da janela
                self.ctrl.log(f"Falha ao criar chave: {e}", "err")
        threading.Thread(target=run, daemon=True).start()

    # ---- atualização: 10 vezes por segundo lê o snapshot do Controller
    def _tick(self):
        self._blink += 1
        snap = self.ctrl.snapshot(self._last_log)
        if snap["log"]:
            self._last_log = snap["log"][-1]["id"]
            for e in snap["log"]:
                color = RED if e["kind"] == "err" else (LED_AMBER if e["kind"] == "evt" else FG)
                self.log.appendHtml(f'<span style="color:{MUTED}">{e["t"]}</span> '
                                    f'<span style="color:{color}">{_html(e["msg"])}</span>')
            self.log.moveCursor(QTextCursor.End)
        if snap["apdu_count"] != self._apdus:
            self._apdus = snap["apdu_count"]
            self._activity_until = time.monotonic() + 0.15
        target = 1.0 if snap["powered"] else 0.0
        if abs(self.key.plugged - target) > 0.01:
            self.key.plugged += (target - self.key.plugged) * 0.35
        else:
            self.key.plugged = target
        self.key.led = self._led(snap)
        self.key.update()
        if snap["version"] != self._version:
            self._version = snap["version"]
            self._fill(snap)
        tr = snap["touch"]
        self.touch_lbl.setText(f"TOQUE NA CHAVE — slot {tr['slot']} · {tr['remaining']:.0f} s" if tr else "")

    def _led(self, snap) -> str:
        if not snap["powered"]:
            return LED_OFF
        if snap["touch"]:
            return LED_GREEN if self._blink % 3 else LED_OFF
        if snap["status"] != "connected":
            return LED_AMBER if (self._blink // 5) % 2 else LED_OFF
        return LED_OFF if time.monotonic() < self._activity_until else LED_GREEN

    def _fill(self, s):
        on = s["powered"]
        self.power.setText("DESLIGAR" if on else "LIGAR")
        self.power.setObjectName("off" if on else "on")
        self.power.style().unpolish(self.power)
        self.power.style().polish(self.power)
        self.info["serial"].setText(str(s["serial"]))
        self.setWindowTitle(f"YubiKey virtual — {s['serial']}")
        shown = (tuple(s["keys"]), s["serial"])
        if shown != self._keys_shown:
            self._keys_shown = shown
            self.keys.blockSignals(True)
            self.keys.clear()
            for k in s["keys"]:
                self.keys.addItem(str(k), k)
            self.keys.setCurrentIndex(self.keys.findData(s["serial"]))
            self.keys.blockSignals(False)
        busy = s["switching"] or not s["multi"]
        self.keys.setEnabled(not busy)
        self.new_btn.setEnabled(not busy)
        full = all(s[k]["remaining"] == s[k]["total"] for k in ("pin", "puk"))
        self.reset_btn.setEnabled(not full and not s["switching"])
        self.setup_btn.setEnabled(not s["setup_running"])
        self.setup_btn.setText("Configurando..." if s["setup_running"] else "Configurar sistema (setup.sh)")
        self.info["firmware"].setText(f"{s['firmware']} [{s['interfaces']}]")
        st = {"connected": "conectada ao pcscd", "disconnected": "desligada"}.get(s["status"], "aguardando o pcscd")
        self.info["status"].setText(f"{st} (vpcd {s['vpcd']})")

        def tries(t):
            txt = (f'<span style="font-family:monospace; font-size:12pt; font-weight:bold">{_html(t["value"])}</span>'
                   f" · {t['remaining']}/{t['total']} tentativas")
            if t["remaining"] == 0:
                txt += f' · <span style="color:{RED}; font-weight:bold">BLOQUEADO</span>'
            return txt + (' · <span style="color:%s">valor padrão</span>' % LED_AMBER if t["default"] else "")
        self.info["pin"].setText(tries(s["pin"]))
        self.info["puk"].setText(tries(s["puk"]))
        m = s["mgmt"]
        self.info["mgmt"].setText(m["type"] + (' · <span style="color:%s">valor padrão</span>' % LED_AMBER
                                               if m["default"] else "") + (" · exige toque" if m["touch"] else ""))
        self.info["apdus"].setText(str(s["apdu_count"]))
        if self.auto.isChecked() != s["auto_touch"]:
            self.auto.setChecked(s["auto_touch"])
        self.slots.setRowCount(0)
        for sl in s["slots"]:
            r = self.slots.rowCount()
            self.slots.insertRow(r)
            for i, k in enumerate(("slot", "type", "origin", "policy")):
                self.slots.setItem(r, i, QTableWidgetItem(sl[k]))

    def closeEvent(self, e):
        self._timer.stop()
        self.ctrl.shutdown()
        e.accept()


def _html(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="yksim.qt", description="Interface nativa (Qt) do simulador de YubiKey")
    ap.add_argument("--serial", type=serial_arg, help="abre esta chave (8 dígitos; cria se não existir)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="diretório das chaves (um <serial>.json por chave)")
    ap.add_argument("--host", default="127.0.0.1", help="host do vpcd")
    ap.add_argument("--port", type=int, default=35963, help="porta do vpcd")
    ap.add_argument("--off", action="store_true", help="iniciar com a chave desligada")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = KeyStore(args.dir)
    card = store.open_default(args.serial)
    state = card.state
    app = QApplication(sys.argv[:1])
    app.setApplicationName("yksim")
    w = Window(Controller(card, args.host, args.port, store), not args.off)
    w.show()
    print(f"yksim: YubiKey 5 NFC ({'.'.join(map(str, state.version))}) Serial: {state.serial} — janela Qt",
          flush=True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
