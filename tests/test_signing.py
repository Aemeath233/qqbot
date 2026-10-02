from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from qqbot.signing import WebhookSigner


def test_official_challenge_vector():
    signer = WebhookSigner("DG5g3B4j9X2KOErG")
    result = signer.challenge("Arq0D5A61EgUu4OxUvOp", "1725442341")
    assert result["signature"] == (
        "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bc"
        "fcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_official_public_key_vector():
    signer = WebhookSigner("naOC0ocQE3shWLAfffVLB1rhYPG7")
    assert list(signer._public_key.public_bytes_raw()) == [
        215,
        195,
        98,
        254,
        120,
        174,
        248,
        31,
        242,
        50,
        135,
        180,
        147,
        98,
        139,
        93,
        176,
        42,
        60,
        79,
        227,
        11,
        33,
        94,
        77,
        25,
        96,
        155,
        93,
        118,
        103,
        58,
    ]


def test_callback_verifies_raw_body_without_reserialization():
    signer = WebhookSigner("naOC0ocQE3shWLAfffVLB1rhYPG7")
    body = b'{ "op": 0,"d": {}, "t": "GATEWAY_EVENT_NAME"}'
    key = Ed25519PrivateKey.from_private_bytes(b"naOC0ocQE3shWLAfffVLB1rhYPG7naOC")
    signature = key.sign(b"1725442341" + body).hex()
    assert signer.verify(body, "1725442341", signature)
    assert not signer.verify(body + b" ", "1725442341", signature)
    assert not signer.verify(body, "1725442342", signature)
    assert not signer.verify(body, "", signature)
    assert not signer.verify(body, "1725442341", "z" * 128)
