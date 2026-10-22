import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shengbo_prelive.security import service_tokens, token_bytes, ServiceSecurityError


def fake_crypto(data, decrypt=False):
    return base64.b64decode(data) if decrypt else base64.b64encode(data)


class SecurityTests(unittest.TestCase):
    def test_encrypted_storage_stable_keys_safe_repr_and_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch("shengbo_prelive.security._dpapi", side_effect=fake_crypto):
            with ThreadPoolExecutor(max_workers=5) as executor:
                tokens = list(executor.map(lambda _: service_tokens(directory), range(5)))
            self.assertTrue(all(item == tokens[0] for item in tokens))
            self.assertEqual(set(tokens[0]), {"agent_to_voice", "controller_to_voice", "voice_to_agent"})
            content = (Path(directory) / "service.dpapi").read_bytes()
            for value in tokens[0].values():
                self.assertNotIn(value, repr(tokens[0]))
                self.assertNotIn(value, str(tokens[0]))
                self.assertNotIn(value.encode(), content)
                self.assertEqual(token_bytes(value), value.encode())
    def test_corrupt_store_not_overwritten_or_printed(self):
        with tempfile.TemporaryDirectory() as directory, patch("shengbo_prelive.security._dpapi", side_effect=fake_crypto):
            path = Path(directory) / "service.dpapi"
            path.write_bytes(b"corrupt-secret")
            with self.assertRaisesRegex(ServiceSecurityError, "service_credential_store_unavailable"):
                service_tokens(directory)
            self.assertEqual(path.read_bytes(), b"corrupt-secret")


if __name__ == "__main__":
    unittest.main()
