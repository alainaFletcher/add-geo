"""load_snowflake_private_key accepts the key in either common encoding.

SNOWFLAKE_PRIVATE_KEY_B64 may be base64 of the whole PEM file, or the PEM
body alone (base64 of the DER key). Encrypted keys need the passphrase.
Errors name the format and the likely cause, never key material.
"""

import base64
import traceback

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import GeocodeAddresses as ga

PASSPHRASE = "test-passphrase"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _expected_der(key):
    return key.private_bytes(serialization.Encoding.DER,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _encoded(key, encoding, passphrase=None):
    encryption = (serialization.BestAvailableEncryption(passphrase.encode())
                  if passphrase else serialization.NoEncryption())
    raw = key.private_bytes(encoding, serialization.PrivateFormat.PKCS8, encryption)
    return base64.b64encode(raw).decode()


def _use(monkeypatch, b64, passphrase=None):
    monkeypatch.setattr(ga, "SNOWFLAKE_PRIVATE_KEY_B64", b64)
    monkeypatch.setattr(ga, "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", passphrase)


@pytest.mark.parametrize("encoding", [serialization.Encoding.PEM, serialization.Encoding.DER],
                         ids=["pem-file", "pem-body-der"])
@pytest.mark.parametrize("passphrase", [None, PASSPHRASE], ids=["plain", "encrypted"])
def test_key_loads_in_either_encoding(monkeypatch, key, encoding, passphrase):
    _use(monkeypatch, _encoded(key, encoding, passphrase), passphrase)
    assert ga.load_snowflake_private_key() == _expected_der(key)


@pytest.mark.parametrize("encoding", [serialization.Encoding.PEM, serialization.Encoding.DER])
def test_encrypted_key_without_passphrase_says_so(monkeypatch, key, encoding):
    _use(monkeypatch, _encoded(key, encoding, PASSPHRASE), None)
    with pytest.raises(ValueError, match="PASSPHRASE is not set"):
        ga.load_snowflake_private_key()


@pytest.mark.parametrize("encoding", [serialization.Encoding.PEM, serialization.Encoding.DER])
def test_wrong_passphrase_says_so_without_leaking(monkeypatch, key, encoding):
    b64 = _encoded(key, encoding, PASSPHRASE)
    _use(monkeypatch, b64, "wrong-passphrase")
    with pytest.raises(ValueError, match="wrong SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") as info:
        ga.load_snowflake_private_key()
    text = "".join(traceback.format_exception(info.value))
    assert b64[:40] not in text and "wrong-passphrase" not in text
    assert info.value.__suppress_context__
