import base64
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from posthog.client import Client
from posthog.exception_signing import (
    KEY_ID_PROPERTY,
    SIGNATURE_PROPERTY,
    VERSION_PROPERTY,
    ExceptionSigner,
    build_canonical,
    derive_key_id,
)
from posthog.test.test_utils import FAKE_TEST_API_KEY

# --- Cross-language parity vector -------------------------------------------------------------
# A fixed Ed25519 keypair (seed = bytes(range(32))) signing a fixed $exception_list. cymbal's
# Rust implementation MUST reproduce CANONICAL_HEX and verify SIGNATURE_B64 under PUBKEY_RAW_B64.
# If either side's canonical encoding or signing drifts, these assertions fail.
SEED = bytes(range(32))
PUBKEY_RAW_B64 = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
KEY_ID = "Vkdap1RjR0wChd9d"
CANONICAL_HEX = (
    "5048455843310a0000000100000009485454504572726f720000001e34303120436c69656e74204572726f72"
    "3a20556e617574686f72697a65640000000200000007726571756573740000001272657175657374732f6d6f"
    "64656c732e707900000004313032310000000f72657175657374732e6d6f64656c730000000b73796e635f73"
    "747269706500000036706f7374686f672f74656d706f72616c2f646174615f696d706f7274732f736f757263"
    "65732f7374726970652f736f757263652e707900000002343200000033706f7374686f672e74656d706f7261"
    "6c2e646174615f696d706f7274732e736f75726365732e7374726970652e736f75726365"
)
SIGNATURE_B64 = "Fyh19k2cC1k9M8cJr54TNH91MDdd67oaUnydyKm7E+QCPN3mK+h3N9Yp5nkM7xYtngD8km7ljqVXARGDmnfzAQ=="

PARITY_EXCEPTION_LIST = [
    {
        "type": "HTTPError",
        "value": "401 Client Error: Unauthorized",
        "stacktrace": {
            "frames": [
                {
                    "function": "request",
                    "filename": "requests/models.py",
                    "lineno": 1021,
                    "module": "requests.models",
                    "in_app": False,
                },
                {
                    "function": "sync_stripe",
                    "filename": "posthog/temporal/data_imports/sources/stripe/source.py",
                    "lineno": 42,
                    "module": "posthog.temporal.data_imports.sources.stripe.source",
                    "in_app": True,
                },
            ]
        },
    }
]


def _private_key_pem(seed=SEED):
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    return sk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class TestCanonical(unittest.TestCase):
    def test_canonical_matches_parity_vector(self):
        self.assertEqual(build_canonical(PARITY_EXCEPTION_LIST).hex(), CANONICAL_HEX)

    def test_canonical_is_deterministic(self):
        self.assertEqual(
            build_canonical(PARITY_EXCEPTION_LIST),
            build_canonical(PARITY_EXCEPTION_LIST),
        )

    def test_excluded_fields_do_not_affect_canonical(self):
        # in_app / abs_path / context are excluded — changing them must not change the bytes.
        mutated = [
            {
                **PARITY_EXCEPTION_LIST[0],
                "stacktrace": {
                    "frames": [
                        {
                            **f,
                            "in_app": not f["in_app"],
                            "abs_path": "/tmp/x",
                            "context_line": "y",
                        }
                        for f in PARITY_EXCEPTION_LIST[0]["stacktrace"]["frames"]
                    ]
                },
            }
        ]
        self.assertEqual(
            build_canonical(mutated), build_canonical(PARITY_EXCEPTION_LIST)
        )

    def test_changing_message_changes_canonical(self):
        mutated = [{**PARITY_EXCEPTION_LIST[0], "value": "different"}]
        self.assertNotEqual(
            build_canonical(mutated), build_canonical(PARITY_EXCEPTION_LIST)
        )

    def test_tolerates_missing_and_malformed(self):
        for case in ([], None, [{}], [{"stacktrace": {}}], [{"value": "x"}]):
            with self.subTest(case=case):
                self.assertTrue(build_canonical(case).startswith(b"PHEXC1\n"))


class TestSigning(unittest.TestCase):
    def test_key_id_matches_parity_vector(self):
        self.assertEqual(derive_key_id(base64.b64decode(PUBKEY_RAW_B64)), KEY_ID)

    def test_signer_key_id_derives_from_private_key(self):
        signer = ExceptionSigner(_private_key_pem())
        self.assertEqual(signer.key_id, KEY_ID)

    def test_signature_matches_parity_vector(self):
        signer = ExceptionSigner(_private_key_pem())
        self.assertEqual(
            signer.sign(build_canonical(PARITY_EXCEPTION_LIST)), SIGNATURE_B64
        )

    def test_signature_verifies_with_public_key(self):
        signer = ExceptionSigner(_private_key_pem())
        canonical = build_canonical(PARITY_EXCEPTION_LIST)
        sig = base64.b64decode(signer.sign(canonical))
        public_key = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
        public_key.verify(sig, canonical)  # raises if invalid

    def test_rejects_non_ed25519_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        with self.assertRaises(ValueError):
            ExceptionSigner(rsa_pem)

    def test_sign_event_attaches_props_for_exceptions(self):
        signer = ExceptionSigner(_private_key_pem())
        event = {
            "event": "$exception",
            "properties": {"$exception_list": PARITY_EXCEPTION_LIST},
        }
        signer.sign_event(event)
        self.assertEqual(event["properties"][SIGNATURE_PROPERTY], SIGNATURE_B64)
        self.assertEqual(event["properties"][KEY_ID_PROPERTY], KEY_ID)
        self.assertEqual(event["properties"][VERSION_PROPERTY], 1)

    def test_sign_event_passes_through_non_exceptions(self):
        signer = ExceptionSigner(_private_key_pem())
        other = {"event": "$pageview", "properties": {}}
        signer.sign_event(other)
        self.assertNotIn(SIGNATURE_PROPERTY, other["properties"])


class TestClientIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_post_patcher = mock.patch("posthog.client.batch_post")
        cls.consumer_post_patcher = mock.patch("posthog.consumer.batch_post")
        cls.client_post_patcher.start()
        cls.consumer_post_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.client_post_patcher.stop()
        cls.consumer_post_patcher.stop()

    def _enqueue_and_read(self, client):
        # _enqueue signs the post-clean() message that actually gets queued, so read it
        # back off the queue rather than the input dict.
        msg = {
            "event": "$exception",
            "properties": {"$exception_list": PARITY_EXCEPTION_LIST},
            "timestamp": None,
        }
        client._enqueue(msg, disable_geoip=True)
        return client.queue.get_nowait()

    def test_client_signs_exception_events(self):
        client = Client(
            FAKE_TEST_API_KEY,
            enable_exception_signing=True,
            exception_signing_private_key=_private_key_pem(),
        )
        self.assertIsNotNone(client._exception_signer)
        queued = self._enqueue_and_read(client)
        self.assertEqual(queued["properties"][SIGNATURE_PROPERTY], SIGNATURE_B64)
        self.assertEqual(queued["properties"][KEY_ID_PROPERTY], KEY_ID)
        self.assertEqual(queued["properties"][VERSION_PROPERTY], 1)

    def test_signing_happens_after_before_send(self):
        # A user's before_send runs before signing, so the signature covers the final content
        # and the callback can't strip it.
        def before_send(event):
            if event.get("event") == "$exception":
                event["properties"][SIGNATURE_PROPERTY] = "attacker-controlled"
            return event

        client = Client(
            FAKE_TEST_API_KEY,
            before_send=before_send,
            enable_exception_signing=True,
            exception_signing_private_key=_private_key_pem(),
        )
        queued = self._enqueue_and_read(client)
        self.assertEqual(queued["properties"][SIGNATURE_PROPERTY], SIGNATURE_B64)

    def test_client_without_signing_adds_no_props(self):
        client = Client(FAKE_TEST_API_KEY)
        self.assertIsNone(client._exception_signer)
        queued = self._enqueue_and_read(client)
        self.assertNotIn(SIGNATURE_PROPERTY, queued["properties"])

    def test_enabled_without_key_warns_and_does_not_sign(self):
        with mock.patch.object(Client, "log") as log:
            client = Client(FAKE_TEST_API_KEY, enable_exception_signing=True)
            self.assertIsNone(client._exception_signer)
            self.assertTrue(
                any("UNSIGNED" in str(c.args) for c in log.warning.call_args_list),
                "expected a warning that events will be sent unsigned",
            )


if __name__ == "__main__":
    unittest.main()
