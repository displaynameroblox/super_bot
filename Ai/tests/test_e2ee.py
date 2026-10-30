import os
import stat
import tempfile
import unittest
from pathlib import Path

from Ai import e2ee


class IdentityTests(unittest.TestCase):
    def test_identity_is_created_once_and_reloaded_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "identity.pem"

            created = e2ee.ServerIdentity.load_or_create(path)
            reloaded = e2ee.ServerIdentity.load_or_create(path)

            self.assertEqual(created.public_key, reloaded.public_key)
            self.assertEqual(created.fingerprint, reloaded.fingerprint)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_loose_permissions_are_tightened_on_load(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.pem"
            e2ee.ServerIdentity.load_or_create(path)
            path.chmod(0o644)

            e2ee.ServerIdentity.load_or_create(path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_foreign_key_type_is_rejected(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.pem"
            path.write_bytes(
                Ed25519PrivateKey.generate().private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

            with self.assertRaises(ValueError):
                e2ee.ServerIdentity.load_or_create(path)

    def test_fingerprint_is_the_sha256_of_the_public_key_and_grouped_for_display(self) -> None:
        identity = e2ee.ServerIdentity.generate()

        self.assertEqual(len(identity.fingerprint), 64)
        self.assertTrue(identity.fingerprint.startswith(identity.key_id.hex()))
        grouped = e2ee.format_fingerprint(identity.fingerprint)
        self.assertEqual(len(grouped.split(" ")), 8)
        self.assertEqual(grouped.replace(" ", "").lower(), identity.fingerprint)
        self.assertEqual(e2ee.format_fingerprint("ab:cd:ef:01"), "ABCDEF01")


class EnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = e2ee.ServerIdentity.generate()
        self.replays = e2ee.ReplayCache()
        self.payload = {
            "method": "POST",
            "path": "/chat",
            "authorization": "Bearer secret-token",
            "body": {"msg": "héllo wörld 👋"},
        }

    def open(self, sealed: bytes, now: float | None = None) -> e2ee.ServerExchange:
        return e2ee.open_request(
            self.identity, sealed, replay_cache=self.replays, now=now
        )

    def test_request_and_response_round_trip(self) -> None:
        sealed, client = e2ee.seal_request(self.identity.public_key, self.payload)

        exchange = self.open(sealed)
        answer = client.open_response(exchange.seal_response({"status": 200, "body": {"ok": 1}}))

        self.assertEqual(exchange.payload, self.payload)
        self.assertEqual(answer, {"status": 200, "body": {"ok": 1}})

    def test_ciphertext_hides_the_content(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)

        for secret in (b"secret-token", b"/chat", "héllo".encode(), b"Bearer"):
            self.assertNotIn(secret, sealed)

    def test_every_request_uses_fresh_keys_and_ids(self) -> None:
        first, _ = e2ee.seal_request(self.identity.public_key, self.payload)
        second, _ = e2ee.seal_request(self.identity.public_key, self.payload)

        self.assertNotEqual(first, second)
        self.assertNotEqual(first[9:41], second[9:41])

    def test_any_bit_flip_is_detected(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)

        for position in (0, 1, 9, 40, 41, len(sealed) // 2, len(sealed) - 1):
            with self.subTest(position=position):
                tampered = bytearray(sealed)
                tampered[position] ^= 0x01
                with self.assertRaises(e2ee.E2EEError):
                    self.open(bytes(tampered))

    def test_truncated_and_oversized_requests_are_rejected(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)

        with self.assertRaises(e2ee.InvalidEnvelope):
            self.open(sealed[:40])
        with self.assertRaises(e2ee.InvalidEnvelope):
            self.open(b"")
        with self.assertRaises(e2ee.EnvelopeTooLarge):
            self.open(b"\x01" * (e2ee.MAX_REQUEST_BYTES + 1))

    def test_a_request_for_another_server_key_is_reported_as_unknown_key(self) -> None:
        other = e2ee.ServerIdentity.generate()
        sealed, _ = e2ee.seal_request(other.public_key, self.payload)

        with self.assertRaises(e2ee.UnknownServerKey):
            self.open(sealed)

    def test_low_order_client_keys_are_rejected(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)
        forged = sealed[:9] + b"\x00" * 32 + sealed[41:]

        with self.assertRaises(e2ee.InvalidEnvelope):
            self.open(forged)

    def test_replayed_requests_are_rejected(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)

        self.open(sealed)
        with self.assertRaises(e2ee.ReplayedRequest):
            self.open(sealed)

    def test_stale_and_future_timestamps_are_rejected_with_the_server_time(self) -> None:
        now = 1_700_000_000
        for offset in (-(e2ee.MAX_CLOCK_SKEW_SECONDS + 1), e2ee.MAX_CLOCK_SKEW_SECONDS + 1):
            with self.subTest(offset=offset):
                sealed, _ = e2ee.seal_request(
                    self.identity.public_key, self.payload, now=now + offset
                )
                with self.assertRaises(e2ee.StaleRequest) as caught:
                    self.open(sealed, now=now)
                self.assertEqual(caught.exception.server_time, now)

    def test_timestamps_inside_the_window_are_accepted(self) -> None:
        now = 1_700_000_000
        sealed, _ = e2ee.seal_request(
            self.identity.public_key,
            self.payload,
            now=now - e2ee.MAX_CLOCK_SKEW_SECONDS,
        )

        self.assertEqual(self.open(sealed, now=now).payload, self.payload)

    def test_rejected_requests_do_not_consume_replay_slots(self) -> None:
        now = 1_700_000_000
        stale, _ = e2ee.seal_request(self.identity.public_key, self.payload, now=1)

        with self.assertRaises(e2ee.StaleRequest):
            self.open(stale, now=now)
        self.assertEqual(self.replays._expiry_by_id, {})

    def test_a_response_only_opens_for_the_request_that_caused_it(self) -> None:
        first_sealed, first_client = e2ee.seal_request(self.identity.public_key, self.payload)
        second_sealed, second_client = e2ee.seal_request(self.identity.public_key, self.payload)
        first_answer = self.open(first_sealed).seal_response({"status": 200, "body": "one"})
        self.open(second_sealed)

        with self.assertRaises(e2ee.InvalidEnvelope):
            second_client.open_response(first_answer)
        self.assertEqual(first_client.open_response(first_answer)["body"], "one")

    def test_a_forged_response_from_another_key_is_rejected(self) -> None:
        _, client = e2ee.seal_request(self.identity.public_key, self.payload)
        impostor = e2ee.ServerIdentity.generate()
        forged_exchange = e2ee.open_request(
            impostor,
            e2ee.seal_request(impostor.public_key, self.payload)[0],
            replay_cache=e2ee.ReplayCache(),
        )

        with self.assertRaises(e2ee.InvalidEnvelope):
            client.open_response(forged_exchange.seal_response({"status": 200, "body": {}}))

    def test_responses_use_a_fresh_key_each_time_even_for_one_request(self) -> None:
        sealed, _ = e2ee.seal_request(self.identity.public_key, self.payload)
        exchange = self.open(sealed)

        first = exchange.seal_response({"status": 200, "body": "same"})
        second = exchange.seal_response({"status": 200, "body": "same"})

        self.assertNotEqual(first[1:33], second[1:33])
        self.assertNotEqual(first[33:], second[33:])

    def test_a_response_can_only_be_opened_once(self) -> None:
        sealed, client = e2ee.seal_request(self.identity.public_key, self.payload)
        answer = self.open(sealed).seal_response({"status": 200, "body": {}})

        client.open_response(answer)
        with self.assertRaises(e2ee.InvalidEnvelope):
            client.open_response(answer)

    def test_oversized_requests_cannot_be_sealed(self) -> None:
        with self.assertRaises(ValueError):
            e2ee.seal_request(
                self.identity.public_key, {"body": "x" * e2ee.MAX_REQUEST_BYTES}
            )
        with self.assertRaises(ValueError):
            e2ee.seal_request(b"short", self.payload)


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        identity = e2ee.ServerIdentity.generate()
        sealed, self.client = e2ee.seal_request(identity.public_key, {"path": "/events/1"})
        self.exchange = e2ee.open_request(identity, sealed, replay_cache=e2ee.ReplayCache())

    def test_events_decrypt_in_order(self) -> None:
        response = self.exchange.seal_response({"status": 200, "body": {}})
        encryptor = self.exchange.stream_encryptor()
        self.client.open_response(response)
        decryptor = self.client.stream_decryptor()

        frames = [encryptor.seal({"type": "message", "n": str(number)}) for number in range(3)]

        self.assertEqual(
            [decryptor.open(frame) for frame in frames],
            [{"type": "message", "n": str(number)} for number in range(3)],
        )
        self.assertNotIn(b"message", frames[0])

    def test_dropped_reordered_replayed_and_forged_frames_are_rejected(self) -> None:
        response = self.exchange.seal_response({"status": 200, "body": {}})
        encryptor = self.exchange.stream_encryptor()
        self.client.open_response(response)
        frames = [encryptor.seal({"n": number}) for number in range(4)]

        for label, order in (("dropped", [1]), ("reordered", [1, 0]), ("replayed", [0, 0])):
            with self.subTest(label=label):
                decryptor = self.client.stream_decryptor()
                with self.assertRaises(e2ee.InvalidEnvelope):
                    for index in order:
                        decryptor.open(frames[index])
        forged = bytearray(frames[0])
        forged[0] ^= 1
        with self.assertRaises(e2ee.InvalidEnvelope):
            self.client.stream_decryptor().open(bytes(forged))

    def test_streams_need_the_response_to_be_exchanged_first(self) -> None:
        identity = e2ee.ServerIdentity.generate()
        sealed, client = e2ee.seal_request(identity.public_key, {})
        exchange = e2ee.open_request(identity, sealed, replay_cache=e2ee.ReplayCache())

        with self.assertRaises(e2ee.InvalidEnvelope):
            exchange.stream_encryptor()
        with self.assertRaises(e2ee.InvalidEnvelope):
            client.stream_decryptor()


class ReplayCacheTests(unittest.TestCase):
    def test_ids_expire_after_the_window(self) -> None:
        cache = e2ee.ReplayCache(window_seconds=10)

        self.assertTrue(cache.add("a" * 32, now=100))
        self.assertFalse(cache.add("a" * 32, now=109))
        self.assertTrue(cache.add("a" * 32, now=111))

    def test_cache_size_is_bounded(self) -> None:
        cache = e2ee.ReplayCache(window_seconds=1000, max_entries=3)

        for number in range(5):
            self.assertTrue(cache.add(f"{number:032d}", now=100 + number))

        self.assertEqual(len(cache._expiry_by_id), 3)
        self.assertTrue(cache.add(f"{0:032d}", now=110))


if __name__ == "__main__":
    unittest.main()
