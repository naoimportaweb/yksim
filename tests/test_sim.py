"""Testes ponta a ponta: yubikit (a biblioteca do ykman) falando com o simulador."""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa, x25519
from yubikit.core import PID, TRANSPORT
from yubikit.core.smartcard import ApduError
from yubikit.management import CAPABILITY, ManagementSession
from yubikit.piv import (
    DEFAULT_MANAGEMENT_KEY,
    KEY_TYPE,
    MANAGEMENT_KEY_TYPE,
    OBJECT_ID,
    PIN_POLICY,
    SLOT,
    TOUCH_POLICY,
    InvalidPinError,
    PivSession,
)
from yubikit.support import get_name, read_info

from yksim import open_sim
from yksim.state import DeviceState
from yksim.testing import SimConnection

SERIAL = 12345678


@pytest.fixture
def card(tmp_path):
    return open_sim(SERIAL, str(tmp_path / "state.json"))


@pytest.fixture
def piv(card):
    return PivSession(SimConnection(card))


def _auth(piv):
    piv.authenticate(DEFAULT_MANAGEMENT_KEY)


def _self_signed(piv, slot, pub):
    """Cria um certificado simples para o slot (como `ykman piv certificates generate`)."""
    from ykman.piv import generate_self_signed_certificate

    now = datetime.datetime.now(datetime.timezone.utc)
    return generate_self_signed_certificate(
        piv, slot, pub, "CN=teste", now, now + datetime.timedelta(days=1)
    )


# ---------------------------------------------------------------- identificação
def test_device_info_matches_ykman_list(card):
    pid = PID.YK4_OTP_FIDO_CCID
    info = read_info(SimConnection(card), pid)
    assert info.serial == SERIAL
    assert info.version == (5, 8, 0)
    assert CAPABILITY.PIV in info.supported_capabilities[TRANSPORT.NFC]
    name = get_name(info, pid.yubikey_type)
    assert name == "YubiKey 5 NFC"
    # Mesma função que o `ykman list` usa para montar a linha
    from types import SimpleNamespace

    from ykman._cli.__main__ import _describe_device

    line = _describe_device(SimpleNamespace(pid=pid), info) + f" Serial: {info.serial}"
    assert line == f"YubiKey 5 NFC (5.8.0) [OTP+FIDO+CCID] Serial: {SERIAL}"


def test_management_session(card):
    m = ManagementSession(SimConnection(card))
    assert m.version == (5, 8, 0)
    assert m.read_device_info().serial == SERIAL


def test_piv_version_serial(piv):
    assert piv.version == (5, 8, 0)
    assert piv.get_serial() == SERIAL
    assert piv.management_key_type == MANAGEMENT_KEY_TYPE.AES192


# ---------------------------------------------------------------- PIN / PUK
def test_pin_flow(piv):
    md = piv.get_pin_metadata()
    assert md.default_value and md.attempts_remaining == 3
    piv.verify_pin("123456")
    with pytest.raises(InvalidPinError) as e:
        piv.verify_pin("000000")
    assert e.value.attempts_remaining == 2
    piv.verify_pin("123456")  # sucesso restaura o contador
    assert piv.get_pin_attempts() == 3
    piv.change_pin("123456", "654321")
    assert not piv.get_pin_metadata().default_value
    piv.verify_pin("654321")


def test_pin_block_and_unblock(piv):
    for _ in range(3):
        with pytest.raises(InvalidPinError):
            piv.verify_pin("999999")
    assert piv.get_pin_attempts() == 0
    with pytest.raises(InvalidPinError):
        piv.verify_pin("123456")
    piv.unblock_pin("12345678", "111111")
    piv.verify_pin("111111")


def test_new_pin_too_short(piv):
    with pytest.raises(ApduError):
        piv.change_pin("123456", "123")


def test_set_pin_attempts(piv):
    _auth(piv)
    piv.verify_pin("123456")
    piv.set_pin_attempts(5, 4)
    md = piv.get_pin_metadata()
    assert (md.total_attempts, md.attempts_remaining) == (5, 5)
    assert piv.get_puk_metadata().total_attempts == 4


# ---------------------------------------------------------------- gerenciamento
def test_wrong_management_key(piv):
    with pytest.raises(ApduError):
        piv.authenticate(b"\x00" * 24)


@pytest.mark.parametrize("kt", list(MANAGEMENT_KEY_TYPE))
def test_change_management_key(card, kt):
    piv = PivSession(SimConnection(card))
    _auth(piv)
    new = bytes(range(kt.key_len))
    piv.set_management_key(kt, new)
    piv2 = PivSession(SimConnection(card))  # nova sessão relê o tipo via metadados
    assert piv2.management_key_type == kt
    piv2.authenticate(new)
    assert not piv2.get_management_key_metadata().default_value


def test_generate_requires_management_key(piv):
    with pytest.raises(ApduError):
        piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)


# ---------------------------------------------------------------- chaves
SIGN_TYPES = [KEY_TYPE.ECCP256, KEY_TYPE.ECCP384, KEY_TYPE.RSA2048, KEY_TYPE.ED25519]


@pytest.mark.parametrize("kt", SIGN_TYPES)
def test_generate_sign_verify(piv, kt):
    _auth(piv)
    pub = piv.generate_key(SLOT.AUTHENTICATION, kt)
    piv.verify_pin("123456")
    msg = b"mensagem de teste"
    if kt == KEY_TYPE.ED25519:
        sig = piv.sign(SLOT.AUTHENTICATION, kt, msg, None)
        pub.verify(sig, msg)
    elif kt.algorithm.name == "RSA":
        sig = piv.sign(SLOT.AUTHENTICATION, kt, msg, hashes.SHA256(), padding.PKCS1v15())
        pub.verify(sig, msg, padding.PKCS1v15(), hashes.SHA256())
    else:
        sig = piv.sign(SLOT.AUTHENTICATION, kt, msg, hashes.SHA256())
        pub.verify(sig, msg, ec.ECDSA(hashes.SHA256()))
    md = piv.get_slot_metadata(SLOT.AUTHENTICATION)
    assert md.key_type == kt and md.generated and md.pin_policy == PIN_POLICY.ONCE


def test_rsa4096_and_3072(piv):
    _auth(piv)
    for kt in (KEY_TYPE.RSA3072, KEY_TYPE.RSA4096):
        pub = piv.generate_key(SLOT.RETIRED1, kt)
        piv.verify_pin("123456")
        sig = piv.sign(SLOT.RETIRED1, kt, b"x", hashes.SHA256(), padding.PSS(padding.MGF1(hashes.SHA256()), 32))
        pub.verify(sig, b"x", padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())


def test_sign_requires_pin(piv):
    _auth(piv)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)
    with pytest.raises(ApduError):
        piv.sign(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, b"x", hashes.SHA256())


def test_pin_policy_always_on_9c(piv):
    _auth(piv)
    piv.generate_key(SLOT.SIGNATURE, KEY_TYPE.ECCP256)
    piv.verify_pin("123456")
    piv.sign(SLOT.SIGNATURE, KEY_TYPE.ECCP256, b"a", hashes.SHA256())
    with pytest.raises(ApduError):  # segunda assinatura sem novo PIN
        piv.sign(SLOT.SIGNATURE, KEY_TYPE.ECCP256, b"b", hashes.SHA256())
    piv.verify_pin("123456")
    piv.sign(SLOT.SIGNATURE, KEY_TYPE.ECCP256, b"b", hashes.SHA256())


def test_card_auth_needs_no_pin(piv):
    _auth(piv)
    piv.generate_key(SLOT.CARD_AUTH, KEY_TYPE.ECCP256)
    PivSession(SimConnection(piv.protocol.connection.card))  # zera estado de sessão
    piv.sign(SLOT.CARD_AUTH, KEY_TYPE.ECCP256, b"x", hashes.SHA256())


def test_touch_policy(card):
    calls = []
    card.applets[1][1]._touch_cb = lambda slot: calls.append(slot) or True
    piv = PivSession(SimConnection(card))
    _auth(piv)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, PIN_POLICY.NEVER, TOUCH_POLICY.ALWAYS)
    piv.sign(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, b"x", hashes.SHA256())
    assert calls == [0x9A]


def test_touch_cached(card):
    calls = []
    card.applets[1][1]._touch_cb = lambda slot: calls.append(slot) or True
    piv = PivSession(SimConnection(card))
    _auth(piv)
    piv.generate_key(SLOT.RETIRED1, KEY_TYPE.ECCP256, PIN_POLICY.NEVER, TOUCH_POLICY.CACHED)
    piv.sign(SLOT.RETIRED1, KEY_TYPE.ECCP256, b"x", hashes.SHA256())
    piv.sign(SLOT.RETIRED1, KEY_TYPE.ECCP256, b"y", hashes.SHA256())
    assert calls == [0x82]  # segundo uso dentro de 15 s não pede toque


def test_touch_denied(card):
    card.applets[1][1]._touch_cb = lambda slot: False
    piv = PivSession(SimConnection(card))
    _auth(piv)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, PIN_POLICY.NEVER, TOUCH_POLICY.ALWAYS)
    with pytest.raises(ApduError):
        piv.sign(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, b"x", hashes.SHA256())


def test_rsa_decrypt(piv):
    _auth(piv)
    pub = piv.generate_key(SLOT.KEY_MANAGEMENT, KEY_TYPE.RSA2048)
    piv.verify_pin("123456")
    ct = pub.encrypt(b"segredo", padding.PKCS1v15())
    assert piv.decrypt(SLOT.KEY_MANAGEMENT, ct, padding.PKCS1v15()) == b"segredo"


@pytest.mark.parametrize("kt", [KEY_TYPE.ECCP256, KEY_TYPE.ECCP384, KEY_TYPE.X25519])
def test_ecdh(piv, kt):
    _auth(piv)
    pub = piv.generate_key(SLOT.KEY_MANAGEMENT, kt)
    piv.verify_pin("123456")
    if kt == KEY_TYPE.X25519:
        peer = x25519.X25519PrivateKey.generate()
        expected = peer.exchange(pub)
    else:
        peer = ec.generate_private_key(pub.curve)
        expected = peer.exchange(ec.ECDH(), pub)
    assert piv.calculate_secret(SLOT.KEY_MANAGEMENT, peer.public_key()) == expected


@pytest.mark.parametrize(
    "key",
    [
        lambda: ec.generate_private_key(ec.SECP256R1()),
        lambda: rsa.generate_private_key(65537, 2048),
        lambda: ed25519.Ed25519PrivateKey.generate(),
    ],
)
def test_import_key(piv, key):
    k = key()
    _auth(piv)
    kt = piv.put_key(SLOT.RETIRED5, k)
    md = piv.get_slot_metadata(SLOT.RETIRED5)
    assert md.key_type == kt and not md.generated
    assert md.public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == k.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(ApduError):  # chave importada não pode ser atestada
        piv.attest_key(SLOT.RETIRED5)


def test_move_and_delete(piv):
    _auth(piv)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)
    piv.move_key(SLOT.AUTHENTICATION, SLOT.RETIRED2)
    piv.get_slot_metadata(SLOT.RETIRED2)
    with pytest.raises(ApduError):
        piv.get_slot_metadata(SLOT.AUTHENTICATION)
    piv.delete_key(SLOT.RETIRED2)
    with pytest.raises(ApduError):
        piv.get_slot_metadata(SLOT.RETIRED2)


# ---------------------------------------------------------------- certificados / atestação
def test_certificate_roundtrip_large(piv):
    _auth(piv)
    pub = piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.RSA4096)
    piv.verify_pin("123456")
    cert = _self_signed(piv, SLOT.AUTHENTICATION, pub)
    piv.put_certificate(SLOT.AUTHENTICATION, cert)  # > 255 bytes: encadeamento
    assert piv.get_certificate(SLOT.AUTHENTICATION) == cert


def test_attestation_chain(card, piv):
    _auth(piv)
    pub = piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256, PIN_POLICY.ALWAYS)
    att = piv.attest_key(SLOT.AUTHENTICATION)
    f9 = piv.get_certificate(SLOT.ATTESTATION)
    ca = x509.load_pem_x509_certificate(card.state.attest_ca_cert_pem.encode())
    att.verify_directly_issued_by(f9)
    f9.verify_directly_issued_by(ca)
    assert att.public_key() == pub
    ext = {e.oid.dotted_string: e.value.value for e in att.extensions}
    assert ext["1.3.6.1.4.1.41482.3.3"] == bytes([5, 8, 0])
    assert ext["1.3.6.1.4.1.41482.3.8"] == bytes([PIN_POLICY.ALWAYS, TOUCH_POLICY.NEVER])
    assert int.from_bytes(ext["1.3.6.1.4.1.41482.3.7"][2:], "big") == SERIAL


def test_objects(piv):
    with pytest.raises(ApduError):
        piv.get_object(OBJECT_ID.CHUID)
    _auth(piv)
    piv.put_object(OBJECT_ID.CHUID, b"\x30\x19abc")
    assert piv.get_object(OBJECT_ID.CHUID) == b"\x30\x19abc"
    piv.put_object(OBJECT_ID.PRINTED, b"protegido")
    s2 = PivSession(SimConnection(piv.protocol.connection.card))
    with pytest.raises(ApduError):
        s2.get_object(OBJECT_ID.PRINTED)
    s2.verify_pin("123456")
    assert s2.get_object(OBJECT_ID.PRINTED) == b"protegido"
    assert piv.get_object(OBJECT_ID.DISCOVERY)


# ---------------------------------------------------------------- reset / persistência
def test_reset(piv):
    _auth(piv)
    piv.set_management_key(MANAGEMENT_KEY_TYPE.AES256, b"\x11" * 32)
    piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)
    piv.change_pin("123456", "999999")
    piv.reset()
    assert piv.get_pin_metadata().default_value
    assert piv.management_key_type == MANAGEMENT_KEY_TYPE.AES192
    with pytest.raises(ApduError):
        piv.get_slot_metadata(SLOT.AUTHENTICATION)
    piv.get_certificate(SLOT.ATTESTATION)  # atestação sobrevive ao reset


def test_persistence(tmp_path):
    path = str(tmp_path / "s.json")
    piv = PivSession(SimConnection(open_sim(SERIAL, path)))
    _auth(piv)
    pub = piv.generate_key(SLOT.AUTHENTICATION, KEY_TYPE.ECCP256)
    piv.change_pin("123456", "246810")
    reloaded = open_sim(state_path=path)
    assert reloaded.state.serial == SERIAL
    piv2 = PivSession(SimConnection(reloaded))
    assert piv2.get_slot_metadata(SLOT.AUTHENTICATION).public_key == pub
    piv2.verify_pin("246810")
    assert DeviceState.load(path).serial == SERIAL
