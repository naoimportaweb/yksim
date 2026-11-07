"""Applet PIV (NIST SP 800-73-4 + extensões Yubico), comportamento de firmware 5.8."""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa, x25519
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import NameOID

try:  # cryptography >= 43 moveu o 3DES para "decrepit"
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # pragma: no cover
    TripleDES = algorithms.TripleDES

from . import tlv
from .errors import SW, ApduError
from .state import DEFAULT_MGMT_KEY, DEFAULT_PIN, DEFAULT_PUK, DeviceState, SlotState

log = logging.getLogger("yksim.piv")

AID = bytes.fromhex("a000000308000010000100")

# Instruções
INS_VERIFY = 0x20
INS_CHANGE_REFERENCE = 0x24
INS_RESET_RETRY = 0x2C
INS_GENERATE_ASYMMETRIC = 0x47
INS_AUTHENTICATE = 0x87
INS_GET_DATA = 0xCB
INS_PUT_DATA = 0xDB
INS_MOVE_KEY = 0xF6
INS_GET_METADATA = 0xF7
INS_GET_SERIAL = 0xF8
INS_ATTEST = 0xF9
INS_SET_PIN_RETRIES = 0xFA
INS_RESET = 0xFB
INS_GET_VERSION = 0xFD
INS_IMPORT_KEY = 0xFE
INS_SET_MGMKEY = 0xFF

# Tipos de chave
RSA1024, RSA2048, RSA3072, RSA4096 = 0x06, 0x07, 0x05, 0x16
ECCP256, ECCP384, ED25519, X25519 = 0x11, 0x14, 0xE0, 0xE1
RSA_BITS = {RSA1024: 1024, RSA2048: 2048, RSA3072: 3072, RSA4096: 4096}
EC_CURVES = {ECCP256: ec.SECP256R1, ECCP384: ec.SECP384R1}
KEY_TYPES = set(RSA_BITS) | set(EC_CURVES) | {ED25519, X25519}

# Chave de gerenciamento
MGMT_TDES, MGMT_AES128, MGMT_AES192, MGMT_AES256 = 0x03, 0x08, 0x0A, 0x0C
MGMT_KEY_LEN = {MGMT_TDES: 24, MGMT_AES128: 16, MGMT_AES192: 24, MGMT_AES256: 32}

# Políticas
PIN_DEFAULT, PIN_NEVER, PIN_ONCE, PIN_ALWAYS = 0, 1, 2, 3
TOUCH_DEFAULT, TOUCH_NEVER, TOUCH_ALWAYS, TOUCH_CACHED = 0, 1, 2, 3

SLOT_CARD_MANAGEMENT = 0x9B
SLOT_ATTESTATION = 0xF9
SLOT_OCC_AUTH = 0x96
KEY_SLOTS = {0x9A, 0x9C, 0x9D, 0x9E, SLOT_ATTESTATION} | set(range(0x82, 0x96))

OBJ_ATTESTATION = 0x5FFF01
OBJ_DISCOVERY = 0x7E
PIN_PROTECTED_OBJECTS = {0x5FC103, 0x5FC108, 0x5FC109, 0x5FC121}
DISCOVERY = bytes.fromhex("7e124f0ba0000003080000100001005f2f024000")

# OIDs de extensão de atestação Yubico (formato do certificado)
OID_FIRMWARE = x509.ObjectIdentifier("1.3.6.1.4.1.41482.3.3")
OID_SERIAL = x509.ObjectIdentifier("1.3.6.1.4.1.41482.3.7")
OID_POLICY = x509.ObjectIdentifier("1.3.6.1.4.1.41482.3.8")
OID_FORM_FACTOR = x509.ObjectIdentifier("1.3.6.1.4.1.41482.3.9")


# --------------------------------------------------------------------------
# Auxiliares de criptografia
# --------------------------------------------------------------------------
def _generate_private_key(key_type: int):
    if key_type in RSA_BITS:
        return rsa.generate_private_key(65537, RSA_BITS[key_type])
    if key_type in EC_CURVES:
        return ec.generate_private_key(EC_CURVES[key_type]())
    if key_type == ED25519:
        return ed25519.Ed25519PrivateKey.generate()
    if key_type == X25519:
        return x25519.X25519PrivateKey.generate()
    raise ApduError(SW.INCORRECT_DATA, "tipo de chave inválido")


def _encode_public_key(key_type: int, pub) -> bytes:
    if key_type in RSA_BITS:
        n = pub.public_numbers()
        return tlv.tlv(0x81, n.n.to_bytes(RSA_BITS[key_type] // 8, "big")) + tlv.tlv(
            0x82, n.e.to_bytes(3, "big")
        )
    if key_type in EC_CURVES:
        return tlv.tlv(
            0x86,
            pub.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            ),
        )
    return tlv.tlv(
        0x86,
        pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
    )


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _load(pem: str):
    return serialization.load_pem_private_key(pem.encode(), None)


def _import_private_key(key_type: int, fields: dict[int, bytes]):
    try:
        if key_type in RSA_BITS:
            p, q, dp, dq, qinv = (int.from_bytes(fields[t], "big") for t in range(1, 6))
            e = 65537
            n = p * q
            if n.bit_length() != RSA_BITS[key_type]:
                raise ApduError(SW.INCORRECT_DATA, "tamanho RSA não confere")
            d = pow(e, -1, (p - 1) * (q - 1))
            return rsa.RSAPrivateNumbers(
                p, q, d, dp, dq, qinv, rsa.RSAPublicNumbers(e, n)
            ).private_key()
        if key_type in EC_CURVES:
            return ec.derive_private_key(
                int.from_bytes(fields[0x06], "big"), EC_CURVES[key_type]()
            )
        if key_type == ED25519:
            return ed25519.Ed25519PrivateKey.from_private_bytes(fields[0x07])
        if key_type == X25519:
            return x25519.X25519PrivateKey.from_private_bytes(fields[0x08])
    except (KeyError, ValueError) as e:
        raise ApduError(SW.INCORRECT_DATA, f"chave inválida: {e}")
    raise ApduError(SW.INCORRECT_P1P2, "tipo de chave inválido")


def _key_type_of(key) -> int:
    if isinstance(key, rsa.RSAPrivateKey):
        return {v: k for k, v in RSA_BITS.items()}[key.key_size]
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return ECCP256 if key.curve.name == "secp256r1" else ECCP384
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return ED25519
    return X25519


def _der_integer(n: int) -> bytes:
    body = n.to_bytes(n.bit_length() // 8 + 1, "big")
    return b"\x02" + bytes([len(body)]) + body


def _sign_cert(builder: x509.CertificateBuilder, key) -> x509.Certificate:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return builder.sign(key, None)
    return builder.sign(key, hashes.SHA256())


def _mgmt_cipher(key_type: int, key: bytes) -> Cipher:
    algo = TripleDES(key) if key_type == MGMT_TDES else algorithms.AES(key)
    return Cipher(algo, modes.ECB())  # noqa: S305 (é o que o protocolo PIV usa)


def _ecb(key_type: int, key: bytes, data: bytes, encrypt: bool) -> bytes:
    c = _mgmt_cipher(key_type, key)
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


# --------------------------------------------------------------------------
# Applet
# --------------------------------------------------------------------------
class PivApplet:
    def __init__(
        self,
        state: DeviceState,
        save: Callable[[], None],
        touch: Callable[[int], bool] | None = None,
    ):
        self.state = state
        self._save = save
        self._touch_cb = touch or (lambda slot: True)
        self._last_touch = -1e9
        self.ensure_attestation()
        self.deselect()

    # ---- ciclo de vida -----------------------------------------------------
    def deselect(self) -> None:
        self.pin_verified = False
        self.pin_fresh = False  # para PIN_POLICY.ALWAYS
        self.mgmt_authenticated = False
        self._witness: bytes | None = None
        self._challenge: bytes | None = None

    def select(self) -> bytes:
        self.deselect()
        # Application Property Template (igual ao de uma YubiKey 5)
        return bytes.fromhex("61114f0600001000010079074f05a000000308")

    # ---- toque ---------------------------------------------------------------
    TOUCH_CACHE_SECONDS = 15

    def _touch(self, slot: int, policy: int = TOUCH_ALWAYS) -> bool:
        now = time.monotonic()
        if policy == TOUCH_CACHED and now - self._last_touch < self.TOUCH_CACHE_SECONDS:
            return True
        ok = bool(self._touch_cb(slot))
        if ok:
            self._last_touch = time.monotonic()
        return ok

    # ---- atestação ---------------------------------------------------------
    def ensure_attestation(self) -> None:
        s = self.state
        if "f9" in s.slots and s.attest_ca_cert_pem:
            return
        log.info("Gerando CA de atestação de TESTE e chave F9")
        now = datetime.datetime.now(datetime.timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP384R1())
        ca_name = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "yksim (TEST ONLY)"),
                x509.NameAttribute(
                    NameOID.COMMON_NAME, "yksim PIV TEST Attestation Root CA"
                ),
            ]
        )
        ca_cert = _sign_cert(
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(datetime.datetime(9999, 12, 31, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True),
            ca_key,
        )
        f9_key = rsa.generate_private_key(65537, 2048)
        f9_name = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "yksim (TEST ONLY)"),
                x509.NameAttribute(
                    NameOID.COMMON_NAME, f"yksim PIV Attestation (serial {s.serial})"
                ),
            ]
        )
        f9_cert = _sign_cert(
            x509.CertificateBuilder()
            .subject_name(f9_name)
            .issuer_name(ca_name)
            .public_key(f9_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(datetime.datetime(9999, 12, 31, tzinfo=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.UnrecognizedExtension(OID_FIRMWARE, bytes(s.version)), critical=False
            ),
            ca_key,
        )
        s.attest_ca_key_pem = _pem(ca_key)
        s.attest_ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
        s.slots["f9"] = SlotState(RSA2048, PIN_NEVER, TOUCH_NEVER, 2, _pem(f9_key))
        s.objects[f"{OBJ_ATTESTATION:x}"] = self._cert_object(f9_cert).hex()
        self._save()

    @staticmethod
    def _cert_object(cert: x509.Certificate) -> bytes:
        return (
            tlv.tlv(0x70, cert.public_bytes(serialization.Encoding.DER))
            + tlv.tlv(0x71, b"\x00")
            + tlv.tlv(0xFE)
        )

    def _f9_cert(self) -> x509.Certificate:
        obj = bytes.fromhex(self.state.objects[f"{OBJ_ATTESTATION:x}"])
        return x509.load_der_x509_certificate(tlv.parse_dict(obj)[0x70])

    # ---- despacho ----------------------------------------------------------
    def process(self, cla: int, ins: int, p1: int, p2: int, data: bytes) -> bytes:
        handler = {
            INS_VERIFY: self._verify,
            INS_CHANGE_REFERENCE: self._change_reference,
            INS_RESET_RETRY: self._reset_retry,
            INS_GENERATE_ASYMMETRIC: self._generate,
            INS_AUTHENTICATE: self._authenticate,
            INS_GET_DATA: self._get_data,
            INS_PUT_DATA: self._put_data,
            INS_MOVE_KEY: self._move_key,
            INS_GET_METADATA: self._get_metadata,
            INS_GET_SERIAL: lambda p1, p2, d: self.state.serial.to_bytes(4, "big"),
            INS_ATTEST: self._attest,
            INS_SET_PIN_RETRIES: self._set_pin_retries,
            INS_RESET: self._reset,
            INS_GET_VERSION: lambda p1, p2, d: bytes(self.state.version),
            INS_IMPORT_KEY: self._import_key,
            INS_SET_MGMKEY: self._set_mgmt_key,
        }.get(ins)
        if handler is None:
            raise ApduError(SW.INS_NOT_SUPPORTED)
        return handler(p1, p2, data)

    # ---- PIN / PUK ---------------------------------------------------------
    @staticmethod
    def _unpad(value: bytes) -> bytes:
        return value.rstrip(b"\xff")

    @staticmethod
    def _check_new(value: bytes) -> bytes:
        v = value.rstrip(b"\xff")
        if not 6 <= len(v) <= 8 or b"\xff" in v:
            raise ApduError(SW.INCORRECT_DATA, "PIN/PUK deve ter 6-8 caracteres")
        return v

    def _check_reference(self, which: str, candidate: bytes) -> None:
        """Valida PIN ou PUK, decrementando o contador em caso de erro."""
        remaining = getattr(self.state, f"{which}_remaining")
        if remaining == 0:
            raise ApduError(SW.AUTH_METHOD_BLOCKED, f"{which.upper()} bloqueado")
        stored = bytes.fromhex(getattr(self.state, which))
        if self._unpad(candidate) != stored:
            remaining -= 1
            setattr(self.state, f"{which}_remaining", remaining)
            self._save()
            if which == "pin":
                self.pin_verified = self.pin_fresh = False
            raise ApduError(0x63C0 | min(remaining, 15), f"{which.upper()} incorreto")
        if remaining != getattr(self.state, f"{which}_total"):
            setattr(self.state, f"{which}_remaining", getattr(self.state, f"{which}_total"))
            self._save()

    def _verify(self, p1: int, p2: int, data: bytes) -> bytes:
        if p2 == SLOT_OCC_AUTH:
            raise ApduError(SW.REFERENCE_NOT_FOUND, "sem biometria")
        if p2 != 0x80:
            raise ApduError(SW.INCORRECT_P1P2)
        if p1 == 0xFF:  # desautenticar
            self.pin_verified = self.pin_fresh = False
            return b""
        if not data:
            if self.pin_verified:
                return b""
            if self.state.pin_remaining == 0:
                raise ApduError(SW.AUTH_METHOD_BLOCKED)
            raise ApduError(0x63C0 | min(self.state.pin_remaining, 15))
        if len(data) != 8:
            raise ApduError(SW.INCORRECT_DATA)
        self._check_reference("pin", data)
        self.pin_verified = self.pin_fresh = True
        return b""

    def _change_reference(self, p1: int, p2: int, data: bytes) -> bytes:
        if p2 not in (0x80, 0x81):
            raise ApduError(SW.INCORRECT_P1P2)
        if len(data) != 16:
            raise ApduError(SW.INCORRECT_DATA)
        which = "pin" if p2 == 0x80 else "puk"
        self._check_reference(which, data[:8])
        new = self._check_new(data[8:])
        setattr(self.state, which, new.hex())
        self._save()
        if which == "pin":
            self.pin_verified = self.pin_fresh = True
        return b""

    def _reset_retry(self, p1: int, p2: int, data: bytes) -> bytes:
        if p2 != 0x80:
            raise ApduError(SW.INCORRECT_P1P2)
        if len(data) != 16:
            raise ApduError(SW.INCORRECT_DATA)
        self._check_reference("puk", data[:8])
        self.state.pin = self._check_new(data[8:]).hex()
        self.state.pin_remaining = self.state.pin_total
        self._save()
        return b""

    def _set_pin_retries(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        if not self.pin_verified:
            raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED)
        if not (1 <= p1 <= 255 and 1 <= p2 <= 255):
            raise ApduError(SW.INCORRECT_P1P2)
        s = self.state
        s.pin_total = s.pin_remaining = p1
        s.puk_total = s.puk_remaining = p2
        s.pin, s.puk = DEFAULT_PIN.hex(), DEFAULT_PUK.hex()
        self._save()
        return b""

    def _reset(self, p1: int, p2: int, data: bytes) -> bytes:
        if self.state.pin_remaining or self.state.puk_remaining:
            raise ApduError(SW.CONDITIONS_NOT_SATISFIED, "PIN e PUK precisam estar bloqueados")
        self.state.reset_piv()
        self._save()
        self.deselect()
        log.info("PIV resetado")
        return b""

    # ---- chave de gerenciamento -------------------------------------------
    def _require_mgmt(self) -> None:
        if not self.mgmt_authenticated:
            raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "requer chave de gerenciamento")

    def _set_mgmt_key(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        if p1 != 0xFF or p2 not in (0xFE, 0xFF) or not data:
            raise ApduError(SW.INCORRECT_P1P2)
        key_type = data[0]
        fields = tlv.parse_dict(data[1:])
        key = fields.get(SLOT_CARD_MANAGEMENT)
        if key_type not in MGMT_KEY_LEN or key is None or len(key) != MGMT_KEY_LEN[key_type]:
            raise ApduError(SW.INCORRECT_DATA)
        s = self.state
        s.mgmt_key_type, s.mgmt_key = key_type, key.hex()
        s.mgmt_touch = TOUCH_ALWAYS if p2 == 0xFE else TOUCH_NEVER
        self._save()
        return b""

    def _auth_mgmt(self, p1: int, items: list[tuple[int, bytes]]) -> bytes:
        s = self.state
        if p1 != s.mgmt_key_type:
            raise ApduError(SW.INCORRECT_P1P2, "tipo de chave de gerenciamento diferente")
        key = bytes.fromhex(s.mgmt_key)
        block = 8 if s.mgmt_key_type == MGMT_TDES else 16
        d = dict(items)

        def enc(x):
            return _ecb(s.mgmt_key_type, key, x, True)

        # 1) Cliente pede witness
        if 0x80 in d and not d[0x80]:
            self._witness = os.urandom(block)
            return tlv.tlv(0x7C, tlv.tlv(0x80, enc(self._witness)))
        # 2) Autenticação mútua: witness decifrado + desafio do cliente
        if d.get(0x80) and d.get(0x81):
            witness, self._witness = self._witness, None
            if witness is None or d[0x80] != witness:
                self.mgmt_authenticated = False
                raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "chave de gerenciamento incorreta")
            if len(d[0x81]) != block:
                raise ApduError(SW.INCORRECT_DATA)
            if s.mgmt_touch == TOUCH_ALWAYS and not self._touch(SLOT_CARD_MANAGEMENT):
                raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "toque negado")
            self.mgmt_authenticated = True
            return tlv.tlv(0x7C, tlv.tlv(0x82, enc(d[0x81])))
        # 3) Autenticação externa (um sentido): cartão envia desafio
        if 0x81 in d and not d[0x81]:
            self._challenge = os.urandom(block)
            return tlv.tlv(0x7C, tlv.tlv(0x81, self._challenge))
        if d.get(0x82):
            challenge, self._challenge = self._challenge, None
            if challenge is None or d[0x82] != enc(challenge):
                self.mgmt_authenticated = False
                raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "chave de gerenciamento incorreta")
            self.mgmt_authenticated = True
            return b""
        raise ApduError(SW.INCORRECT_DATA)

    # ---- GENERAL AUTHENTICATE ---------------------------------------------
    def _authenticate(self, p1: int, p2: int, data: bytes) -> bytes:
        try:
            outer = tlv.parse_dict(data)
            items = tlv.parse(outer[0x7C])
        except (KeyError, ValueError, IndexError):
            raise ApduError(SW.INCORRECT_DATA)
        if p2 == SLOT_CARD_MANAGEMENT:
            return self._auth_mgmt(p1, items)
        return self._use_key(p1, p2, dict(items))

    def _check_pin_policy(self, slot: SlotState) -> None:
        if slot.pin_policy == PIN_NEVER:
            return
        if not self.pin_verified:
            raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "PIN não verificado")
        if slot.pin_policy == PIN_ALWAYS:
            if not self.pin_fresh:
                raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "PIN exigido a cada uso")
            self.pin_fresh = False

    def _use_key(self, p1: int, p2: int, d: dict[int, bytes]) -> bytes:
        slot = self.state.slots.get(f"{p2:02x}")
        if slot is None or p2 == SLOT_ATTESTATION:
            raise ApduError(SW.INCORRECT_DATA, "slot vazio")
        if p1 != slot.key_type:
            raise ApduError(SW.INCORRECT_DATA, "algoritmo não confere com o slot")
        if 0x82 not in d or (0x81 not in d and 0x85 not in d):
            raise ApduError(SW.INCORRECT_DATA)
        self._check_pin_policy(slot)
        if slot.touch_policy in (TOUCH_ALWAYS, TOUCH_CACHED) and not self._touch(
            p2, slot.touch_policy
        ):
            raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED, "toque negado")

        key = _load(slot.private_pem)
        kt = slot.key_type
        try:
            if 0x85 in d:  # ECDH
                peer = d[0x85]
                if kt in EC_CURVES:
                    pub = ec.EllipticCurvePublicKey.from_encoded_point(EC_CURVES[kt](), peer)
                    out = key.exchange(ec.ECDH(), pub)
                elif kt == X25519:
                    out = key.exchange(x25519.X25519PublicKey.from_public_bytes(peer))
                else:
                    raise ApduError(SW.INCORRECT_DATA)
            else:
                msg = d[0x81]
                if kt in RSA_BITS:  # operação RSA "crua" (assinatura ou decifração)
                    ln = RSA_BITS[kt] // 8
                    if len(msg) != ln:
                        raise ApduError(SW.INCORRECT_DATA)
                    pn = key.private_numbers()
                    m = int.from_bytes(msg, "big")
                    if m >= pn.public_numbers.n:
                        raise ApduError(SW.INCORRECT_DATA)
                    out = pow(m, pn.d, pn.public_numbers.n).to_bytes(ln, "big")
                elif kt in EC_CURVES:
                    h = {32: hashes.SHA256(), 48: hashes.SHA384()}.get(len(msg))
                    if h is None:
                        raise ApduError(SW.INCORRECT_DATA)
                    out = key.sign(msg, ec.ECDSA(Prehashed(h)))
                elif kt == ED25519:
                    out = key.sign(msg)
                else:
                    raise ApduError(SW.INCORRECT_DATA, "X25519 não assina")
        except ValueError as e:
            raise ApduError(SW.INCORRECT_DATA, str(e))
        log.info("Uso de chave no slot %02X (%d bytes)", p2, len(out))
        return tlv.tlv(0x7C, tlv.tlv(0x82, out))

    # ---- geração / importação ---------------------------------------------
    @staticmethod
    def _resolve_policies(slot: int, pin: int, touch: int) -> tuple[int, int]:
        if pin in (4, 5):  # MATCH_* exige YubiKey Bio
            raise ApduError(SW.INCORRECT_DATA, "política biométrica não suportada")
        if pin not in (PIN_DEFAULT, PIN_NEVER, PIN_ONCE, PIN_ALWAYS):
            raise ApduError(SW.INCORRECT_DATA)
        if touch not in (TOUCH_DEFAULT, TOUCH_NEVER, TOUCH_ALWAYS, TOUCH_CACHED):
            raise ApduError(SW.INCORRECT_DATA)
        if pin == PIN_DEFAULT:
            pin = {0x9C: PIN_ALWAYS, 0x9E: PIN_NEVER, SLOT_ATTESTATION: PIN_NEVER}.get(slot, PIN_ONCE)
        if touch == TOUCH_DEFAULT:
            touch = TOUCH_NEVER
        return pin, touch

    def _store_key(self, slot: int, key, pin: int, touch: int, origin: int) -> SlotState:
        pin, touch = self._resolve_policies(slot, pin, touch)
        st = SlotState(_key_type_of(key), pin, touch, origin, _pem(key))
        self.state.slots[f"{slot:02x}"] = st
        self._save()
        return st

    def _generate(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        if p2 not in KEY_SLOTS or p2 == SLOT_ATTESTATION:
            raise ApduError(SW.INCORRECT_P1P2)
        try:
            params = tlv.parse_dict(tlv.parse_dict(data)[0xAC])
            key_type = params[0x80][0]
        except (KeyError, IndexError, ValueError):
            raise ApduError(SW.INCORRECT_DATA)
        if key_type not in KEY_TYPES:
            raise ApduError(SW.INCORRECT_DATA, "algoritmo não suportado")
        pin = params.get(0xAA, b"\x00")[0]
        touch = params.get(0xAB, b"\x00")[0]
        self._resolve_policies(p2, pin, touch)  # valida antes de gastar tempo gerando
        key = _generate_private_key(key_type)
        self._store_key(p2, key, pin, touch, origin=1)
        log.info("Chave %02X gerada no slot %02X", key_type, p2)
        return tlv.tlv(0x7F49, _encode_public_key(key_type, key.public_key()))

    def _import_key(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        if p2 not in KEY_SLOTS or p1 not in KEY_TYPES:
            raise ApduError(SW.INCORRECT_P1P2)
        try:
            fields = tlv.parse_dict(data)
        except ValueError:
            raise ApduError(SW.INCORRECT_DATA)
        key = _import_private_key(p1, fields)
        pin = fields.get(0xAA, b"\x00")[0]
        touch = fields.get(0xAB, b"\x00")[0]
        self._store_key(p2, key, pin, touch, origin=2)
        log.info("Chave %02X importada no slot %02X", p1, p2)
        return b""

    def _move_key(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        slots = self.state.slots
        src = f"{p2:02x}"
        if p2 not in KEY_SLOTS or p2 == SLOT_ATTESTATION:
            raise ApduError(SW.INCORRECT_P1P2)
        if src not in slots:
            raise ApduError(SW.REFERENCE_NOT_FOUND, "slot de origem vazio")
        if p1 == 0xFF:  # apagar
            del slots[src]
        else:
            if p1 not in KEY_SLOTS or p1 == SLOT_ATTESTATION or p1 == p2:
                raise ApduError(SW.INCORRECT_P1P2)
            slots[f"{p1:02x}"] = slots.pop(src)
        self._save()
        return b""

    # ---- metadados ----------------------------------------------------------
    def _get_metadata(self, p1: int, p2: int, data: bytes) -> bytes:
        s = self.state
        if p2 in (0x80, 0x81):
            which = "pin" if p2 == 0x80 else "puk"
            default = DEFAULT_PIN if which == "pin" else DEFAULT_PUK
            return (
                tlv.tlv(0x01, b"\xff")
                + tlv.tlv(0x05, b"\x01" if bytes.fromhex(getattr(s, which)) == default else b"\x00")
                + tlv.tlv(0x06, bytes([getattr(s, f"{which}_total"), getattr(s, f"{which}_remaining")]))
            )
        if p2 == SLOT_CARD_MANAGEMENT:
            return (
                tlv.tlv(0x01, bytes([s.mgmt_key_type]))
                + tlv.tlv(0x02, bytes([0x00, s.mgmt_touch]))
                + tlv.tlv(0x05, b"\x01" if bytes.fromhex(s.mgmt_key) == DEFAULT_MGMT_KEY else b"\x00")
            )
        slot = s.slots.get(f"{p2:02x}")
        if p2 not in KEY_SLOTS or slot is None:
            raise ApduError(SW.REFERENCE_NOT_FOUND)
        pub = _load(slot.private_pem).public_key()
        return (
            tlv.tlv(0x01, bytes([slot.key_type]))
            + tlv.tlv(0x02, bytes([slot.pin_policy, slot.touch_policy]))
            + tlv.tlv(0x03, bytes([slot.origin]))
            + tlv.tlv(0x04, _encode_public_key(slot.key_type, pub))
        )

    def _attest(self, p1: int, p2: int, data: bytes) -> bytes:
        slot = self.state.slots.get(f"{p1:02x}")
        if slot is None or p1 == SLOT_ATTESTATION:
            raise ApduError(SW.REFERENCE_NOT_FOUND)
        if slot.origin != 1:
            raise ApduError(SW.INCORRECT_DATA, "só chaves geradas no dispositivo podem ser atestadas")
        f9 = _load(self.state.slots["f9"].private_pem)
        issuer = self._f9_cert().subject
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = _sign_cert(
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"YubiKey PIV Attestation {p1:02x}")]))
            .issuer_name(issuer)
            .public_key(_load(slot.private_pem).public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(datetime.datetime(9999, 12, 31, tzinfo=datetime.timezone.utc))
            .add_extension(x509.UnrecognizedExtension(OID_FIRMWARE, bytes(self.state.version)), critical=False)
            .add_extension(x509.UnrecognizedExtension(OID_SERIAL, _der_integer(self.state.serial)), critical=False)
            .add_extension(x509.UnrecognizedExtension(OID_POLICY, bytes([slot.pin_policy, slot.touch_policy])), critical=False)
            .add_extension(x509.UnrecognizedExtension(OID_FORM_FACTOR, bytes([self.state.form_factor])), critical=False),
            f9,
        )
        return cert.public_bytes(serialization.Encoding.DER)

    # ---- objetos de dados --------------------------------------------------
    @staticmethod
    def _object_id(data: bytes) -> int:
        try:
            return int.from_bytes(tlv.parse_dict(data)[0x5C], "big")
        except (KeyError, ValueError):
            raise ApduError(SW.INCORRECT_DATA)

    def _get_data(self, p1: int, p2: int, data: bytes) -> bytes:
        if (p1, p2) != (0x3F, 0xFF):
            raise ApduError(SW.INCORRECT_P1P2)
        obj = self._object_id(data)
        if obj == OBJ_DISCOVERY:
            return DISCOVERY
        if obj in PIN_PROTECTED_OBJECTS and not self.pin_verified:
            raise ApduError(SW.SECURITY_CONDITION_NOT_SATISFIED)
        value = self.state.objects.get(f"{obj:x}")
        if value is None:
            raise ApduError(SW.FILE_NOT_FOUND)
        return tlv.tlv(0x53, bytes.fromhex(value))

    def _put_data(self, p1: int, p2: int, data: bytes) -> bytes:
        self._require_mgmt()
        if (p1, p2) != (0x3F, 0xFF):
            raise ApduError(SW.INCORRECT_P1P2)
        try:
            fields = tlv.parse_dict(data)
            obj = int.from_bytes(fields[0x5C], "big")
            value = fields[0x53]
        except (KeyError, ValueError):
            raise ApduError(SW.INCORRECT_DATA)
        if obj == OBJ_DISCOVERY:
            raise ApduError(SW.INCORRECT_DATA)
        if len(value) > 3052:
            raise ApduError(SW.NO_SPACE)
        if value:
            self.state.objects[f"{obj:x}"] = value.hex()
        else:
            self.state.objects.pop(f"{obj:x}", None)
        self._save()
        return b""
