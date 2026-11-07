"""Nomes legíveis para log/painel (compartilhado pelas interfaces)."""

INS_NAMES = {
    0xA4: "SELECT",
    0x20: "VERIFY PIN",
    0x24: "CHANGE PIN/PUK",
    0x2C: "UNBLOCK PIN",
    0x47: "GENERATE KEY",
    0x87: "GENERAL AUTHENTICATE",
    0xCB: "GET DATA",
    0xDB: "PUT DATA",
    0xF6: "MOVE/DELETE KEY",
    0xF7: "GET METADATA",
    0xF8: "GET SERIAL",
    0xF9: "ATTEST",
    0xFA: "SET PIN RETRIES",
    0xFB: "RESET PIV",
    0xFD: "GET VERSION",
    0xFE: "IMPORT KEY",
    0xFF: "SET MGMT KEY",
    0x1D: "READ CONFIG",
    0x1C: "WRITE CONFIG",
    0xC0: "GET RESPONSE",
}
KEY_TYPE_NAMES = {
    0x06: "RSA1024", 0x07: "RSA2048", 0x05: "RSA3072", 0x16: "RSA4096",
    0x11: "ECCP256", 0x14: "ECCP384", 0xE0: "Ed25519", 0xE1: "X25519",
}
MGMT_NAMES = {0x03: "TDES", 0x08: "AES128", 0x0A: "AES192", 0x0C: "AES256"}
PIN_POLICY_NAMES = {1: "PIN nunca", 2: "PIN 1x", 3: "PIN sempre"}
TOUCH_POLICY_NAMES = {1: "", 2: ", toque sempre", 3: ", toque cache"}
