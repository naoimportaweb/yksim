#!/usr/bin/env bash
# Prepara o Debian (ou o Ubuntu do WSL2) para o yksim: pcscd + leitor virtual vpcd + venv Python.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d /run/systemd/system ]; then
  echo "!! systemd não está ativo; o pcscd precisa dele." >&2
  if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "   No WSL: crie /etc/wsl.conf com as linhas abaixo, rode 'wsl --shutdown' no PowerShell e abra o WSL de novo:" >&2
    printf '     [boot]\n     systemd=true\n' >&2
  fi
  exit 1
fi

# Nome do leitor: o ykman só lista leitores que contenham "Yubico YubiKey", e
# deduz o modo USB ([OTP+FIDO+CCID]) a partir desse nome.
READER_NAME="${READER_NAME:-Yubico YubiKey OTP+FIDO+CCID (yksim)}"

echo ">> Instalando pcscd, vsmartcard-vpcd e utilitários..."
PKGS=(pcscd pcsc-tools vsmartcard-vpcd python3-venv python3-tk opensc)
sudo apt-get update -qq
# Janela Qt: usa o PySide6 do sistema se a distribuição tiver o pacote (Debian 13 tem); senão, vai para o venv.
if apt-cache show python3-pyside6.qtwidgets >/dev/null 2>&1; then
  PKGS+=(python3-pyside6.qtwidgets)
fi
sudo apt-get install -y -qq "${PKGS[@]}"

CONF=/etc/reader.conf.d/vpcd
if [ -f "$CONF" ]; then
  echo ">> Renomeando o leitor virtual para: $READER_NAME"
  sudo sed -i "s|^FRIENDLYNAME .*|FRIENDLYNAME \"$READER_NAME\"|" "$CONF"
else
  echo "!! $CONF não encontrado; o leitor ficará como 'Virtual PCD'" >&2
fi

echo ">> Reiniciando pcscd..."
# Por padrão o pcscd sai após 60 s ocioso (--auto-exit), o que "desplugaria" a chave
# virtual. Mantém o pcscd sempre ativo.
sudo mkdir -p /etc/systemd/system/pcscd.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/sbin/pcscd --foreground $PCSCD_ARGS\n' |
  sudo tee /etc/systemd/system/pcscd.service.d/yksim.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable pcscd.service >/dev/null 2>&1 || true
sudo systemctl restart pcscd.socket pcscd.service

if [ ! -x .venv/bin/python ]; then
  echo ">> Criando venv..."
  python3 -m venv --system-site-packages .venv   # usa o PySide6 do sistema/usuário (janela Qt)
fi
.venv/bin/pip install -q -r requirements.txt
if ! .venv/bin/python -c "import PySide6.QtWidgets" 2>/dev/null; then
  echo ">> PySide6 não veio do sistema: instalando no venv..."
  .venv/bin/pip install -q PySide6
fi

mkdir -p "$HOME/.local/bin"
chmod +x bin/yubikey
ln -sf "$PWD/bin/yubikey" "$HOME/.local/bin/yubikey"

echo
echo "Pronto. Abra o simulador com:  yubikey"
echo "e teste com:  .venv/bin/ykman list"
