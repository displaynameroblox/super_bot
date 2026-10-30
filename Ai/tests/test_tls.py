import datetime
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from Ai import tls


class SelfSignedCertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "server"

    def certificate(self, material: tls.TlsMaterial) -> x509.Certificate:
        return x509.load_pem_x509_certificate(material.certfile.read_bytes())

    def test_a_certificate_is_generated_for_loopback_and_local_names(self) -> None:
        material = tls.prepare_server_tls(directory=self.directory)

        certificate = self.certificate(material)
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        self.assertTrue(material.generated)
        self.assertTrue(material.self_signed)
        self.assertIn("localhost", names.get_values_for_type(x509.DNSName))
        addresses = {str(address) for address in names.get_values_for_type(x509.IPAddress)}
        self.assertTrue({"127.0.0.1", "::1"} <= addresses)
        self.assertIsInstance(certificate.public_key(), ec.EllipticCurvePublicKey)
        self.assertEqual(
            tls.certificate_fingerprint(certificate.public_bytes(serialization.Encoding.DER)),
            material.fingerprint,
        )

    def test_the_certificate_is_a_plain_server_certificate_not_a_ca(self) -> None:
        certificate = self.certificate(tls.prepare_server_tls(directory=self.directory))

        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        self.assertFalse(constraints.value.ca)
        self.assertTrue(constraints.critical)
        self.assertEqual(list(usage.value), [ExtendedKeyUsageOID.SERVER_AUTH])
        validity = certificate.not_valid_after_utc - certificate.not_valid_before_utc
        self.assertGreater(validity.days, 3000)

    def test_private_material_is_only_readable_by_the_owner(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        material = tls.prepare_server_tls(directory=self.directory)

        self.assertEqual(stat.S_IMODE(material.keyfile.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_the_certificate_is_reused_until_it_is_close_to_expiring(self) -> None:
        first = tls.prepare_server_tls(directory=self.directory)
        second = tls.prepare_server_tls(directory=self.directory)
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=tls.CERTIFICATE_VALIDITY_DAYS - 5
        )
        renewed = tls.prepare_server_tls(directory=self.directory, now=soon)

        self.assertFalse(second.generated)
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertTrue(renewed.generated)
        self.assertNotEqual(renewed.fingerprint, first.fingerprint)

    def test_mismatched_or_corrupt_files_are_replaced(self) -> None:
        first = tls.prepare_server_tls(directory=self.directory)
        other_key = ec.generate_private_key(ec.SECP256R1())
        first.keyfile.write_bytes(
            other_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        replaced = tls.prepare_server_tls(directory=self.directory)
        replaced.certfile.write_text("not a certificate")
        regenerated = tls.prepare_server_tls(directory=self.directory)

        self.assertTrue(replaced.generated)
        self.assertTrue(regenerated.generated)
        self.assertNotEqual(replaced.fingerprint, regenerated.fingerprint)


class ConfiguredCertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.generated = tls.prepare_server_tls(directory=self.directory / "generated")

    def test_a_configured_certificate_is_used_as_is(self) -> None:
        material = tls.prepare_server_tls(
            directory=self.directory / "unused",
            certfile=str(self.generated.certfile),
            keyfile=str(self.generated.keyfile),
        )

        self.assertFalse(material.generated)
        self.assertEqual(material.certfile, self.generated.certfile)
        self.assertEqual(material.fingerprint, self.generated.fingerprint)
        self.assertFalse((self.directory / "unused").exists())

    def test_certificate_and_key_must_be_configured_together(self) -> None:
        for options in ({"certfile": "cert.pem"}, {"keyfile": "key.pem"}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    tls.prepare_server_tls(directory=self.directory, **options)

    def test_missing_or_mismatched_files_fail_with_a_clear_error(self) -> None:
        other = tls.prepare_server_tls(directory=self.directory / "other")
        cases = {
            "missing certificate": (self.directory / "none.pem", self.generated.keyfile),
            "missing key": (self.generated.certfile, self.directory / "none.pem"),
            "key of another certificate": (self.generated.certfile, other.keyfile),
        }
        for label, (certfile, keyfile) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    tls.prepare_server_tls(certfile=str(certfile), keyfile=str(keyfile))


class DataDirectoryTests(unittest.TestCase):
    def test_server_directory_can_be_overridden(self) -> None:
        with patch.dict(os.environ, {"SUPER_BOT_SERVER_DIR": "/srv/super-bot-keys"}):
            self.assertEqual(tls.server_data_dir(), Path("/srv/super-bot-keys"))

    def test_server_directory_defaults_below_the_app_data_directory(self) -> None:
        with patch.dict(os.environ, {"SUPER_BOT_SERVER_DIR": ""}):
            self.assertEqual(tls.server_data_dir(), tls.app_data_dir() / "server")

    def test_certificate_der_round_trips_pem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            material = tls.prepare_server_tls(directory=Path(directory))
            pem = material.certfile.read_text()

            self.assertEqual(
                tls.certificate_fingerprint(tls.certificate_der(pem)),
                material.fingerprint,
            )
            self.assertEqual(tls.certificate_der(pem), tls.certificate_der(pem.encode()))


if __name__ == "__main__":
    unittest.main()
