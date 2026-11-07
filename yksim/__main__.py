"""CLI: python -m yksim [--serial 39072602 | --new | --list]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .card import VirtualYubiKey
from .keys import DEFAULT_DIR, KeyStore, serial_arg
from .state import DeviceState
from . import vpcd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="yksim",
        description="Simulador de YubiKey 5 NFC (5.8.0) com PIV, exposto ao PC/SC via vpcd.",
    )
    ap.add_argument("--serial", type=serial_arg, help="abre esta chave (8 dígitos; cria se não existir)")
    ap.add_argument("--new", action="store_true", help="cria uma chave nova com serial aleatório de 8 dígitos")
    ap.add_argument("--list", action="store_true", help="lista as chaves do diretório e sai")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="diretório das chaves (um <serial>.json por chave)")
    ap.add_argument("--state", metavar="ARQ", help="usa este arquivo de estado em vez do diretório de chaves")
    ap.add_argument("--host", default="127.0.0.1", help="host do vpcd")
    ap.add_argument("--port", type=int, default=35963, help="porta do vpcd")
    ap.add_argument(
        "--touch",
        choices=["auto", "deny", "prompt"],
        default="auto",
        help="como responder quando uma chave exige toque",
    )
    ap.add_argument("--export-ca", metavar="ARQ", help="grava a CA de atestação de teste (PEM) e sai")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.new and args.serial:
        ap.error("use --serial ou --new, não os dois")

    if args.state:  # arquivo avulso, fora do diretório de chaves
        path = args.state
        if os.path.exists(path):
            state = DeviceState.load(path)
            if args.serial and args.serial != state.serial:
                ap.error(f"{path} é a chave {state.serial}, não {args.serial}")
        else:
            state = DeviceState(serial=args.serial) if args.serial else DeviceState()
    else:
        store = KeyStore(args.dir)
        if args.list:
            last = store.last_used()
            for k in store.serials():
                print(f"{k}{'  (última usada)' if k == last else ''}")
            return 0
        if args.new:
            serial = store.create()
            print(f"Chave nova criada: serial {serial}")
        else:
            serial = args.serial or store.last_used()
            if serial is None or not store.exists(serial):
                serial = store.create(serial)
        path = store.path(serial)
        state = store.load(serial)

    def touch(slot: int) -> bool:
        if args.touch == "auto":
            print(f"[yksim] toque simulado (slot {slot:02X})", file=sys.stderr)
            return True
        if args.touch == "deny":
            return False
        ans = input(f"[yksim] Toque solicitado para o slot {slot:02X}. Aceitar? [S/n] ")
        return ans.strip().lower() in ("", "s", "y", "sim", "yes")

    card = VirtualYubiKey(state, path, touch)

    if args.export_ca:  # não regrava o estado (o simulador pode estar rodando)
        with open(args.export_ca, "w") as f:
            f.write(state.attest_ca_cert_pem)
        print(f"CA de atestação de teste gravada em {args.export_ca}")
        return 0

    state.save(path)
    v = ".".join(map(str, state.version))
    print(f"yksim: YubiKey 5 NFC ({v}) [OTP+FIDO+CCID] Serial: {state.serial}")
    print(f"Estado: {path}")
    print(f"Conectando ao vpcd em {args.host}:{args.port} (Ctrl+C para sair)")
    try:
        vpcd.serve(card, args.host, args.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
