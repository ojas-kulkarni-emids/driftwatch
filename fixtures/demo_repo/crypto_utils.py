"""Third demo fixture -- deliberately chosen because pyca/cryptography
publishes ZERO GitHub Releases (confirmed live), so get_changelog returns
nothing at all for any version range of this library. That's the forcing
function for search_github_issues: there is no changelog text to reason
about, only real-world reports.

Real, verified fact: `algorithms.Blowfish` in
cryptography.hazmat.primitives.ciphers.algorithms has carried a deprecation
warning since 3.0, and as of 43.0.0 that warning states it will be REMOVED
from this module in 45.0.0 (moved to .decrepit.ciphers.algorithms instead).
As of 46.0.3 (checked live) it is still importable from the original path --
another case of a stated removal date that hasn't actually happened, same
shape as the urllib3 case, not cherry-picked to look dramatic.

encrypt_legacy() is touched by this bump's deprecation trajectory;
encrypt_modern() uses AES, entirely unrelated to the Blowfish/decrepit
migration, and should be judged untouched.
"""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def encrypt_legacy(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.Blowfish(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_modern(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()
