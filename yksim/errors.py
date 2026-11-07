"""Status words ISO 7816 usados pelo simulador."""


class SW:
    OK = 0x9000
    WRONG_LENGTH = 0x6700
    SECURITY_CONDITION_NOT_SATISFIED = 0x6982
    AUTH_METHOD_BLOCKED = 0x6983
    CONDITIONS_NOT_SATISFIED = 0x6985
    COMMAND_NOT_ALLOWED = 0x6986
    INCORRECT_DATA = 0x6A80
    FUNCTION_NOT_SUPPORTED = 0x6A81
    FILE_NOT_FOUND = 0x6A82
    NO_SPACE = 0x6A84
    INCORRECT_P1P2 = 0x6A86
    REFERENCE_NOT_FOUND = 0x6A88
    WRONG_P1P2 = 0x6B00
    INS_NOT_SUPPORTED = 0x6D00
    CLA_NOT_SUPPORTED = 0x6E00
    UNKNOWN = 0x6F00


class ApduError(Exception):
    def __init__(self, sw: int, msg: str = ""):
        super().__init__(msg or f"SW={sw:04X}")
        self.sw = sw
