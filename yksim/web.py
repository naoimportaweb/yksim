"""Interface web do yksim (abre no navegador padrão).

    python -m yksim.web            -> http://localhost:8765
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import Controller
from .keys import DEFAULT_DIR, KeyStore, serial_arg

log = logging.getLogger("yksim.web")
UI_FILE = Path(__file__).with_name("web_ui.html")


def make_handler(ctrl: Controller):
    page = UI_FILE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silencia o log padrão
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            # Proteção contra DNS rebinding: só aceita Host local
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("localhost", "127.0.0.1", "[::1]")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._json(ctrl.snapshot())
            if self.path == "/api/events":
                return self._events()
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            # Exige JSON: impede que outro site dispare ações via formulário
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, b"json required", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, b"bad json", "text/plain")
            if self.path == "/api/power":
                on = body.get("on")
                threading.Thread(
                    target=ctrl.set_power, args=(not ctrl.powered if on is None else bool(on),), daemon=True
                ).start()
            elif self.path == "/api/touch":
                ctrl.touch()
            elif self.path == "/api/auto_touch":
                ctrl.set_auto_touch(body.get("value", False))
            elif self.path == "/api/key":
                serial = body.get("serial")
                if not isinstance(serial, int) or serial not in ctrl.snapshot()["keys"]:
                    return self._send(400, b"unknown key", "text/plain")
                threading.Thread(target=ctrl.select_key, args=(serial,), daemon=True).start()
            elif self.path == "/api/reset_attempts":
                ctrl.reset_attempts()
            elif self.path == "/api/new_key":
                threading.Thread(target=ctrl.new_key, daemon=True).start()
            else:
                return self._send(404, b"not found", "text/plain")
            self._json({"ok": True})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_log = 0
            version = -1
            try:
                while True:
                    snap = ctrl.snapshot(last_log)
                    if snap["log"]:
                        last_log = snap["log"][-1]["id"]
                    version = snap["version"]
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                    self.wfile.flush()
                    # contagem regressiva do toque precisa de atualização periódica
                    ctrl.wait_change(version, 0.25 if snap["touch"] else 15.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return Handler


def open_browser(url: str) -> None:
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="yksim.web", description="Interface web do simulador de YubiKey")
    ap.add_argument("--serial", type=serial_arg, help="abre esta chave (8 dígitos; cria se não existir)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="diretório das chaves (um <serial>.json por chave)")
    ap.add_argument("--host", default="127.0.0.1", help="host do vpcd")
    ap.add_argument("--port", type=int, default=35963, help="porta do vpcd")
    ap.add_argument("--http-port", type=int, default=8765, help="porta da interface web")
    ap.add_argument("--off", action="store_true", help="iniciar com a chave desligada")
    ap.add_argument("--no-browser", action="store_true", help="não abrir o navegador")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    store = KeyStore(args.dir)
    card = store.open_default(args.serial)
    state = card.state

    ctrl = Controller(card, args.host, args.port, store)
    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(ctrl))
    server.daemon_threads = True
    url = f"http://localhost:{args.http_port}/"
    print(f"yksim: YubiKey 5 NFC ({'.'.join(map(str, state.version))}) Serial: {state.serial}")
    print(f"Interface: {url}  (Ctrl+C para sair)", flush=True)

    if not args.off:
        ctrl.set_power(True)
    if not args.no_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
