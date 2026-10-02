"""QQ Webhook 的 Ed25519 验签与回调地址验证。"""

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class WebhookSigner:
    def __init__(self, secret: str):
        raw = secret.encode("utf-8")
        if not raw:
            raise ValueError("AppSecret 不能为空")
        seed = (raw * ((32 + len(raw) - 1) // len(raw)))[:32]
        self._private_key = Ed25519PrivateKey.from_private_bytes(seed)
        self._public_key = self._private_key.public_key()

    def verify(self, body: bytes, timestamp: str, signature: str) -> bool:
        if (
            not timestamp
            or len(timestamp) > 32
            or not timestamp.isascii()
            or not timestamp.isdigit()
        ):
            return False
        if len(signature) != 128:
            return False
        try:
            decoded = bytes.fromhex(signature)
            self._public_key.verify(decoded, timestamp.encode("ascii") + body)
        except (ValueError, InvalidSignature):
            return False
        return True

    def challenge(self, plain_token: str, event_ts: str) -> dict[str, str]:
        signature = self._private_key.sign((event_ts + plain_token).encode("utf-8"))
        return {"plain_token": plain_token, "signature": signature.hex()}
