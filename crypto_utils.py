"""SUPERBOT v5.5.36 - Encryption Utilities"""
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def get_fernet(master_key: str = None) -> Fernet:
    """Create Fernet instance from master key"""
    if master_key is None:
        master_key = os.environ.get('MASTER_KEY')

    if not master_key:
        raise ValueError("MASTER_KEY environment variable is required")

    # Use random salt derived from master key itself (not fixed)
    # This is still deterministic per master_key but not a hardcoded weak point
    salt = hashes.Hash(hashes.SHA256())
    salt.update(master_key.encode())
    salt_bytes = salt.finalize()[:16]

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt_bytes,
        iterations=480000,  # Increased from 100000 for better security
    )
    key = base64.urlsafe_b64encode(kdf.derive(master_key.encode()))
    return Fernet(key)


def encrypt_value(value: str, master_key: str = None) -> str:
    """Encrypt a string value"""
    if not value:
        return ""
    f = get_fernet(master_key)
    return f.encrypt(value.encode()).decode()


def decrypt_value(encrypted_value: str, master_key: str = None) -> str:
    """Decrypt an encrypted string value"""
    if not encrypted_value:
        return ""
    f = get_fernet(master_key)
    return f.decrypt(encrypted_value.encode()).decode()
