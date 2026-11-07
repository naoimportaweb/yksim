"""Várias chaves num diretório (KeyStore) e troca de chave no Controller."""

import argparse
import os
import time

import pytest
from yubikit.piv import DEFAULT_MANAGEMENT_KEY, KEY_TYPE, SLOT, PivSession

from yksim import open_sim
from yksim.controller import Controller
from yksim.keys import KeyStore, serial_arg
from yksim.state import DeviceState, random_serial
from yksim.testing import SimConnection


def _is_8_digits(n: int) -> bool:
    return len(str(n)) == 8


def test_random_serial_has_8_digits():
    assert all(_is_8_digits(random_serial()) for _ in range(2000))
    assert _is_8_digits(DeviceState().serial)
    assert _is_8_digits(open_sim().state.serial)


def test_random_serial_avoids_existing(monkeypatch):
    draws = iter([0, 0, 45_555_555])  # sorteia 10000000 duas vezes (ocupado), depois 55555555
    monkeypatch.setattr("yksim.state.secrets.randbelow", lambda n: next(draws))
    assert random_serial(exclude={10_000_000}) == 55_555_555


def test_serial_arg():
    assert serial_arg("39072602") == 39072602
    for bad in ("1234567", "123456789", "01234567", "abc", "-1234567"):
        with pytest.raises(argparse.ArgumentTypeError):
            serial_arg(bad)


def test_create_and_list(tmp_path):
    store = KeyStore(str(tmp_path))
    assert store.serials() == [] and store.last_used() is None
    a, b = store.create(), store.create()
    assert a != b and _is_8_digits(a) and _is_8_digits(b)
    assert store.serials() == sorted([a, b])
    assert os.path.exists(tmp_path / f"{a}.json")
    assert store.load(a).serial == a
    assert "f9" in store.load(a).slots  # cada chave tem a própria atestação
    assert store.load(a).attest_ca_cert_pem != store.load(b).attest_ca_cert_pem
    with pytest.raises(FileExistsError):
        store.create(a)


def test_open_default_uses_last_used(tmp_path):
    store = KeyStore(str(tmp_path))
    a, b = store.create(), store.create()
    store.open(a)
    os.utime(store.path(b), (time.time() - 60,) * 2)
    assert store.last_used() == a
    assert store.open_default().state.serial == a
    assert store.open_default(b).state.serial == b
    assert store.last_used() == b


def test_open_default_creates_when_empty(tmp_path):
    store = KeyStore(str(tmp_path))
    card = store.open_default()
    assert _is_8_digits(card.state.serial)
    assert store.serials() == [card.state.serial]


def test_legacy_state_is_migrated(tmp_path):
    DeviceState(serial=39072602).save(str(tmp_path / "state.json"))
    store = KeyStore(str(tmp_path))
    assert not os.path.exists(tmp_path / "state.json")
    assert store.serials() == [39072602]


def test_keys_are_independent(tmp_path):
    store = KeyStore(str(tmp_path))
    a, b = store.create(), store.create()
    piv = PivSession(SimConnection(store.open(a)))
    piv.authenticate(DEFAULT_MANAGEMENT_KEY)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)
    assert "9a" in store.load(a).slots
    assert "9a" not in store.load(b).slots


def test_controller_switch_and_new_key(tmp_path):
    store = KeyStore(str(tmp_path))
    a = store.create()
    ctrl = Controller(store.open(a), store=store)
    assert ctrl.snapshot()["keys"] == [a]

    b = ctrl.new_key()
    snap = ctrl.snapshot()
    assert _is_8_digits(b) and snap["serial"] == b and snap["keys"] == sorted([a, b])
    assert ctrl.client.card is ctrl.card
    assert ctrl.card.applets[1][1]._touch_cb == ctrl._touch_callback

    ctrl.select_key(a)
    assert ctrl.snapshot()["serial"] == a
    assert PivSession(SimConnection(ctrl.card)).get_pin_attempts() == 3
    assert not ctrl.powered  # estava desligada, continua desligada


def test_controller_reset_attempts(tmp_path):
    from yubikit.piv import InvalidPinError

    store = KeyStore(str(tmp_path))
    ctrl = Controller(store.open(store.create()), store=store)
    piv = PivSession(SimConnection(ctrl.card))
    for _ in range(3):
        with pytest.raises(InvalidPinError):
            piv.verify_pin("000000")
    assert piv.get_pin_attempts() == 0
    ctrl.card.state.puk_remaining = 0
    ctrl.reset_attempts()
    assert piv.get_pin_attempts() == 3
    piv.verify_pin("123456")  # PIN não mudou
    saved = store.load(ctrl.card.state.serial)
    assert (saved.pin_remaining, saved.puk_remaining) == (3, 3)


def test_controller_run_setup(tmp_path, monkeypatch):
    script = tmp_path / "setup.sh"
    script.write_text('echo ">> passo 1"\necho "!! aviso"\nexit 3\n')
    ctrl = Controller(open_sim())
    monkeypatch.setattr(Controller, "_sudo_ok", staticmethod(lambda: True))
    assert ctrl.run_setup(script) == 3
    msgs = [(e["msg"], e["kind"]) for e in ctrl.snapshot()["log"]]
    assert (">> passo 1", "") in msgs and ("!! aviso", "err") in msgs
    assert ("setup.sh falhou (código 3)", "err") in msgs
    assert not ctrl.setup_running

    monkeypatch.setattr(Controller, "_sudo_ok", staticmethod(lambda: False))
    assert ctrl.run_setup(script) is None
    assert "sudo pede senha" in ctrl.snapshot()["log"][-1]["msg"]
