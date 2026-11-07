"""Interface gráfica do yksim: chave virtual com botão liga/desliga e disco de toque.

    python -m yksim.gui --serial 39072602

- Botão "Ligar/Desligar": pluga/desconecta a chave do leitor virtual (vpcd/pcscd).
- Disco dourado: clique (ou barra de espaço) = tocar na chave.
- LED: apagado = desligada; âmbar piscando = aguardando o vpcd/pcscd;
  verde fixo = conectada (pisca a cada APDU); verde piscando rápido = aguardando toque.
"""

from __future__ import annotations

import argparse
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .card import VirtualYubiKey
from .keys import DEFAULT_DIR, KeyStore, serial_arg
from .state import DEFAULT_MGMT_KEY, DEFAULT_PIN, DEFAULT_PUK
from .vpcd import VpcdClient

TOUCH_TIMEOUT = 15.0  # segundos, como na chave física

from .names import INS_NAMES, KEY_TYPE_NAMES, MGMT_NAMES, PIN_POLICY_NAMES, TOUCH_POLICY_NAMES  # noqa: E402

# Paleta
BG = "#1e2227"
PANEL = "#262b31"
FG = "#e6e6e6"
MUTED = "#9aa3ad"
BODY = "#141414"
BODY_EDGE = "#3a3a3a"
METAL = "#c9ccd1"
METAL_DARK = "#8d9198"
GOLD = "#d4a93c"
GOLD_PRESSED = "#a8832a"
GOLD_EDGE = "#8a6a1f"
LED_OFF = "#3b3221"
LED_GREEN = "#5dff7a"
LED_AMBER = "#ffb347"

PLUG_OFFSET = 70  # quanto a chave desce quando desplugada


class App:
    def __init__(self, root: tk.Tk, card: VirtualYubiKey, host: str, port: int, start_on: bool):
        self.root = root
        self.card = card
        self.host, self.port = host, port
        self.events: queue.Queue = queue.Queue()

        self.status = "disconnected"
        self.powered = False
        self.auto_touch = False  # espelho thread-safe da checkbox
        self._touch_lock = threading.Lock()
        self._touch_req: dict | None = None
        self._last_activity = 0.0
        self._pressed = False
        self._key_y = PLUG_OFFSET  # começa desplugada (posição)

        card.on_transmit = lambda a, r: self.events.put(("apdu", a, r))
        card.applets[1][1]._touch_cb = self._touch_callback
        self.client = VpcdClient(card, host, port, on_status=lambda s: self.events.put(("status", s)))

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<space>", lambda e: self._touch())
        self.root.bind("<Control-p>", lambda e: self.toggle_power())

        self._tick()
        self._refresh_info()
        if start_on:
            self.root.after(200, self.toggle_power)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        r = self.root
        r.title(f"yksim — YubiKey 5 NFC virtual ({self.card.state.serial})")
        r.configure(bg=BG)
        r.minsize(760, 600)

        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=FG, font=("DejaVu Sans", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("DejaVu Sans", 13, "bold"))
        style.configure("Touch.TLabel", foreground=LED_GREEN, font=("DejaVu Sans", 11, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.map("TCheckbutton", background=[("active", PANEL)])
        for name, bg, active in (("On", "#2e7d32", "#388e3c"), ("Off", "#b23b3b", "#c94848")):
            style.configure(f"{name}.TButton", font=("DejaVu Sans", 12, "bold"), padding=(14, 10),
                            background=bg, foreground="white", borderwidth=0)
            style.map(f"{name}.TButton", background=[("pressed", bg), ("active", active)])

        outer = ttk.Frame(r, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # --- coluna esquerda: a chave -----------------------------------------
        left = ttk.Frame(outer, style="Panel.TFrame", padding=12)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self.canvas = tk.Canvas(left, width=240, height=470, bg=PANEL, highlightthickness=0)
        self.canvas.pack()
        self._draw_key()

        self.power_btn = ttk.Button(left, text="LIGAR", style="On.TButton", command=self.toggle_power)
        self.power_btn.pack(fill="x", pady=(10, 0))
        ttk.Label(
            left,
            text="Clique no disco dourado (ou Espaço)\npara tocar na chave",
            style="Muted.TLabel",
            justify="center",
        ).pack(pady=(8, 0))

        # --- coluna direita: informações + log ------------------------------
        right = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(11, weight=1)

        ttk.Label(right, text="YubiKey 5 NFC (virtual)", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self.info: dict[str, ttk.Label] = {}
        rows = [
            ("status", "Estado"),
            ("serial", "Serial"),
            ("fw", "Firmware"),
            ("iface", "Interfaces USB"),
            ("reader", "vpcd"),
            ("pin", "PIN"),
            ("puk", "PUK"),
            ("mgmt", "Chave de gerenciamento"),
            ("slots", "Chaves PIV"),
        ]
        for i, (k, label) in enumerate(rows, start=1):
            ttk.Label(right, text=label + ":", style="Muted.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 10))
            lbl = ttk.Label(right, text="—", justify="left")
            lbl.grid(row=i, column=1, sticky="w")
            self.info[k] = lbl

        self.touch_lbl = ttk.Label(right, text="", style="Touch.TLabel")
        self.touch_lbl.grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 4))

        log_frame = ttk.Frame(right, style="Panel.TFrame")
        log_frame.grid(row=11, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame, height=12, bg="#16191d", fg="#c8d0d8", insertbackground=FG,
            relief="flat", font=("DejaVu Sans Mono", 9), state="disabled", wrap="none",
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = sb.set
        self.log.tag_configure("err", foreground="#ff8a80")
        self.log.tag_configure("evt", foreground=LED_GREEN)

        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            right, text="Aprovar toques automaticamente", variable=self.auto_var,
            command=lambda: setattr(self, "auto_touch", self.auto_var.get()),
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))

    @staticmethod
    def _round_rect(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return c.create_polygon(pts, smooth=True, **kw)

    def _draw_key(self) -> None:
        c = self.canvas
        cx = 120
        # "Notebook" com a porta USB no topo
        c.create_rectangle(0, 0, 240, 34, fill="#3d434b", outline="")
        c.create_rectangle(80, 22, 160, 34, fill="#0c0c0c", outline="#5a6068")
        c.create_text(120, 11, text="porta USB", fill=MUTED, font=("DejaVu Sans", 8))

        y0 = 20  # topo do conector quando plugada
        t = "key"
        # Conector USB-A
        c.create_rectangle(cx - 36, y0, cx + 36, y0 + 72, fill=METAL, outline=METAL_DARK, width=2, tags=t)
        for dx in (-14, 14):
            c.create_rectangle(cx + dx - 6, y0 + 18, cx + dx + 6, y0 + 28, fill=METAL_DARK, outline="", tags=t)
        # Corpo
        self._round_rect(c, cx - 58, y0 + 66, cx + 58, y0 + 360, 26, fill=BODY, outline=BODY_EDGE, width=2, tags=t)
        c.create_text(cx, y0 + 112, text="yksim", fill="#5f6670", font=("DejaVu Sans", 14, "bold"), tags=t)
        c.create_text(cx, y0 + 134, text="5 NFC · virtual", fill="#4d535b", font=("DejaVu Sans", 9), tags=t)
        # Argola
        c.create_oval(cx - 18, y0 + 318, cx + 18, y0 + 354, fill=PANEL, outline=BODY_EDGE, width=2, tags=t)
        # LED (anel em volta do disco) + disco dourado de toque
        dy = y0 + 225
        self.led_glow = c.create_oval(cx - 50, dy - 50, cx + 50, dy + 50, fill=BODY, outline="", tags=t)
        self.led = c.create_oval(cx - 43, dy - 43, cx + 43, dy + 43, fill=LED_OFF, outline="", tags=t)
        self.disc = c.create_oval(cx - 36, dy - 36, cx + 36, dy + 36, fill=GOLD, outline=GOLD_EDGE, width=2, tags=(t, "touch"))
        self.disc_ring = c.create_oval(cx - 22, dy - 22, cx + 22, dy + 22, outline=GOLD_EDGE, width=2, tags=(t, "touch"))
        self.disc_dot = c.create_oval(cx - 6, dy - 6, cx + 6, dy + 6, fill=GOLD_EDGE, outline="", tags=(t, "touch"))

        c.tag_bind("touch", "<ButtonPress-1>", self._on_press)
        c.tag_bind("touch", "<ButtonRelease-1>", self._on_release)
        c.tag_bind("touch", "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind("touch", "<Leave>", lambda e: c.configure(cursor=""))
        c.move("key", 0, self._key_y)
        # A placa do notebook fica na frente do conector
        c.tag_raise("all")
        c.tag_lower("key")

    # ------------------------------------------------------------------ energia
    def toggle_power(self) -> None:
        if self.powered:
            self.powered = False
            self._cancel_touch()
            threading.Thread(target=self.client.stop, daemon=True).start()
            self._animate_plug(PLUG_OFFSET)
            self.power_btn.configure(text="LIGAR", style="On.TButton")
            self._log("Chave desligada (desplugada)", "evt")
        else:
            self.powered = True
            self.card.power_on()
            self.client.start()
            self._animate_plug(0)
            self.power_btn.configure(text="DESLIGAR", style="Off.TButton")
            self._log("Chave ligada (plugada)", "evt")

    def _animate_plug(self, target: int) -> None:
        step = 7 if target > self._key_y else -7

        def frame():
            if self._key_y == target:
                return
            d = step if abs(target - self._key_y) > abs(step) else target - self._key_y
            self._key_y += d
            self.canvas.move("key", 0, d)
            self.root.after(12, frame)

        frame()

    # ------------------------------------------------------------------ toque
    def _touch_callback(self, slot: int) -> bool:
        """Chamado na thread do vpcd quando uma operação exige toque."""
        if self.auto_touch:
            self.events.put(("log", f"Toque aprovado automaticamente (slot {slot:02X})", "evt"))
            return True
        req = {"slot": slot, "event": threading.Event(), "ok": False, "deadline": time.monotonic() + TOUCH_TIMEOUT}
        with self._touch_lock:
            self._touch_req = req
        self.events.put(("log", f"Aguardando toque (slot {slot:02X})...", "evt"))
        req["event"].wait(TOUCH_TIMEOUT)
        with self._touch_lock:
            self._touch_req = None
        self.events.put(("log", "Toque confirmado" if req["ok"] else "Toque não recebido (tempo esgotado/cancelado)",
                         "evt" if req["ok"] else "err"))
        return req["ok"]

    def _cancel_touch(self) -> None:
        with self._touch_lock:
            if self._touch_req:
                self._touch_req["event"].set()

    def _touch(self) -> None:
        if not self.powered:
            self._log("Chave desligada — toque ignorado", "err")
            return
        with self._touch_lock:
            req = self._touch_req
            if req:
                req["ok"] = True
                req["event"].set()
                return
        self._log("Toque (nenhuma operação aguardando; OTP não é emulado)")

    def _on_press(self, _e) -> None:
        self._pressed = True
        for item in (self.disc, self.disc_ring, self.disc_dot):
            self.canvas.move(item, 1, 1)
        self.canvas.itemconfigure(self.disc, fill=GOLD_PRESSED)

    def _on_release(self, _e) -> None:
        if not self._pressed:
            return
        self._pressed = False
        for item in (self.disc, self.disc_ring, self.disc_dot):
            self.canvas.move(item, -1, -1)
        self.canvas.itemconfigure(self.disc, fill=GOLD)
        self._touch()

    # ------------------------------------------------------------------ laço da UI
    def _tick(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "apdu":
                    self._last_activity = time.monotonic()
                    self._log_apdu(ev[1], ev[2])
                elif kind == "status":
                    if self.status != ev[1]:
                        self.status = ev[1]
                        if ev[1] == "connected":
                            self._log(f"Conectada ao pcscd via vpcd ({self.host}:{self.port})", "evt")
                elif kind == "log":
                    self._log(ev[1], ev[2] if len(ev) > 2 else "")
        except queue.Empty:
            pass
        self._update_led()
        self._update_touch_label()
        self.root.after(40, self._tick)

    def _update_led(self) -> None:
        now = time.monotonic()
        with self._touch_lock:
            touching = self._touch_req is not None
        if not self.powered:
            color = LED_OFF
        elif touching:
            color = LED_GREEN if int(now * 6) % 2 == 0 else LED_OFF  # pisca rápido
        elif self.status != "connected":
            color = LED_AMBER if int(now * 2) % 2 == 0 else LED_OFF
        elif now - self._last_activity < 0.06:
            color = LED_OFF  # piscada de atividade
        else:
            color = LED_GREEN
        self.canvas.itemconfigure(self.led, fill=color)
        glow = {LED_GREEN: "#1f3d26", LED_AMBER: "#3d2f1a"}.get(color, BODY)
        self.canvas.itemconfigure(self.led_glow, fill=glow)

    def _update_touch_label(self) -> None:
        with self._touch_lock:
            req = self._touch_req
        if req:
            left = max(0, req["deadline"] - time.monotonic())
            self.touch_lbl.configure(text=f"► Toque na chave! (slot {req['slot']:02X}, {left:4.1f}s)")
        else:
            self.touch_lbl.configure(text="")

    def _refresh_info(self) -> None:
        s = self.card.state
        status = {
            "connected": "Ligada — conectada ao pcscd",
            "connecting": "Ligada — aguardando vpcd/pcscd...",
            "disconnected": "Desligada",
        }.get(self.status, self.status)
        if not self.powered:
            status = "Desligada"
        self.info["status"].configure(text=status)
        self.info["serial"].configure(text=str(s.serial))
        self.info["fw"].configure(text=".".join(map(str, s.version)))
        self.info["iface"].configure(text="OTP+FIDO+CCID")
        self.info["reader"].configure(text=f"{self.host}:{self.port}")

        def tries(which, default):
            is_def = bytes.fromhex(getattr(s, which)) == default
            rem, tot = getattr(s, f"{which}_remaining"), getattr(s, f"{which}_total")
            txt = f"{rem}/{tot} tentativas" + (" (valor padrão)" if is_def else "")
            return txt + (" — BLOQUEADO" if rem == 0 else "")

        self.info["pin"].configure(text=tries("pin", DEFAULT_PIN))
        self.info["puk"].configure(text=tries("puk", DEFAULT_PUK))
        mk = MGMT_NAMES.get(s.mgmt_key_type, hex(s.mgmt_key_type))
        if bytes.fromhex(s.mgmt_key) == DEFAULT_MGMT_KEY:
            mk += " (valor padrão)"
        if s.mgmt_touch == 2:
            mk += ", exige toque"
        self.info["mgmt"].configure(text=mk)
        slots = []
        for k in sorted(s.slots):
            if k == "f9":
                continue
            st = s.slots[k]
            pol = PIN_POLICY_NAMES.get(st.pin_policy, "") + TOUCH_POLICY_NAMES.get(st.touch_policy, "")
            origin = "gerada" if st.origin == 1 else "importada"
            slots.append(f"{k.upper()}: {KEY_TYPE_NAMES.get(st.key_type, hex(st.key_type))} ({origin}; {pol})")
        self.info["slots"].configure(text="\n".join(slots) or "nenhuma")
        self.root.after(500, self._refresh_info)

    # ------------------------------------------------------------------ log
    def _log(self, msg: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def _log_apdu(self, apdu: bytes, resp: bytes) -> None:
        if len(apdu) < 4:
            return
        ins = apdu[1]
        if apdu[0] & 0x10:  # bloco intermediário de encadeamento
            return
        name = INS_NAMES.get(ins, f"INS {ins:02X}")
        if ins == 0xA4:
            aid = apdu[5:5 + apdu[4]].hex() if len(apdu) > 5 else ""
            name += {"a000000308": " PIV", "a000000527471117": " Management"}.get(aid[:16], "") or (
                " PIV" if aid.startswith("a000000308") else f" {aid}"
            )
        sw = int.from_bytes(resp[-2:], "big")
        ok = sw == 0x9000 or sw >> 8 == 0x61
        self._log(f"{name:<24} → {sw:04X}", "" if ok else "err")

    def _on_close(self) -> None:
        self._cancel_touch()
        if self.client.running:
            self.client.stop()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="yksim.gui", description="Interface gráfica do simulador de YubiKey")
    ap.add_argument("--serial", type=serial_arg, help="abre esta chave (8 dígitos; cria se não existir)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="diretório das chaves (um <serial>.json por chave)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=35963)
    ap.add_argument("--off", action="store_true", help="iniciar com a chave desligada")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    # A janela Tk (antiga) não troca de chave: abre a escolhida/última usada. Use a Qt ou a web para trocar.
    card = KeyStore(args.dir).open_default(args.serial)

    root = tk.Tk()
    App(root, card, args.host, args.port, start_on=not args.off)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
