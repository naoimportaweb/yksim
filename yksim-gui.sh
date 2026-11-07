#!/usr/bin/env bash
# Abre a interface gráfica do simulador (Tk).
cd "$(dirname "$0")"
exec .venv/bin/python -m yksim.gui "$@"
