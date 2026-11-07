import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from Ai import phone_model, tls


class PhoneModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "PHONE_MODEL_BASE_URL": "http://192.168.1.42:8080/v1",
                "TEST_PHONE_API_KEY": "test-key",
                "PHONE_MODEL_NAME": "test-model",
                "PHONE_MODEL_TIMEOUT_SECONDS": "120",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_api_model_name_defaults_to_stable_server_alias(self) -> None:
        os.environ.pop("PHONE_MODEL_NAME")

        self.assertEqual(phone_model._configuration()[2], "superbot-phone")

    def test_phone_probe_requires_the_configured_model(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {"data": [{"id": "test-model"}]}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.get.return_value = response

        with patch("Ai.phone_model.httpx.Client", return_value=client):
            phone_model.probe()

        self.assertEqual(
            client.get.call_args.args[0],
            "http://192.168.1.42:8080/v1/models",
        )
        self.assertEqual(
            client.get.call_args.kwargs["headers"]["Authorization"],
            "Bearer test-key",
        )

    def test_phone_probe_rejects_an_unavailable_model(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {"data": [{"id": "other-model"}]}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.get.return_value = response

        with patch("Ai.phone_model.httpx.Client", return_value=client):
            with self.assertRaisesRegex(phone_model.PhoneModelError, "test-model"):
                phone_model.probe()

    def test_chat_sends_bearer_auth_history_and_json_schema(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {
            "choices": [{"message": {"content": "  Hello there.  "}}]
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        schema = {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
        }

        with patch("Ai.phone_model.httpx.Client", return_value=client) as client_factory:
            result = phone_model.chat(
                [
                    {"role": "system", "content": "Be friendly."},
                    {"role": "user", "content": "Hi"},
                ],
                response_schema=schema,
            )

        self.assertEqual(result, "Hello there.")
        client_factory.assert_called_once()
        request = client.post.call_args
        self.assertEqual(
            request.args[0],
            "http://192.168.1.42:8080/v1/chat/completions",
        )
        self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(request.kwargs["json"]["model"], "test-model")
        self.assertEqual(
            request.kwargs["json"]["messages"],
            [
                {"role": "system", "content": "Be friendly."},
                {"role": "user", "content": "Hi"},
            ],
        )
        self.assertEqual(
            request.kwargs["json"]["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "super_bot_response",
                    "schema": schema,
                },
            },
        )
        self.assertEqual(request.kwargs["json"]["max_tokens"], 512)
        self.assertEqual(
            request.kwargs["json"]["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertFalse(request.kwargs["json"]["stream"])

    def test_gemini_provider_sends_chat_and_structured_schema(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "private model reasoning", "thought": True},
                            {"text": '{"reply":"Hello","is_coding_request":false}'}
                        ]
                    }
                }
            ]
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        schema = {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "is_coding_request": {"type": "boolean"},
            },
            "required": ["reply", "is_coding_request"],
            "additionalProperties": False,
        }
        messages = [
            {"role": "system", "content": "Be friendly."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": "How are you?"},
        ]

        with (
            patch.dict(
                os.environ,
                {
                    "PHONE_MODEL_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "cloud-key",
                    "GEMINI_MODEL_NAME": "gemini-test",
                    "PHONE_MODEL_TIMEOUT_SECONDS": "120",
                },
                clear=True,
            ),
            patch("Ai.phone_model.httpx.Client", return_value=client) as factory,
        ):
            result = phone_model.chat(messages, response_schema=schema)

        self.assertEqual(result, '{"reply":"Hello","is_coding_request":false}')
        self.assertEqual(factory.call_args.kwargs["trust_env"], False)
        request = client.post.call_args
        self.assertEqual(
            request.args[0],
            (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-test:generateContent"
            ),
        )
        self.assertEqual(request.kwargs["headers"], {"x-goog-api-key": "cloud-key"})
        payload = request.kwargs["json"]
        self.assertEqual(
            payload["systemInstruction"],
            {"parts": [{"text": "Be friendly."}]},
        )
        self.assertEqual(
            payload["contents"],
            [
                {"role": "user", "parts": [{"text": "Hi"}]},
                {"role": "model", "parts": [{"text": "Hello."}]},
                {"role": "user", "parts": [{"text": "How are you?"}]},
            ],
        )
        self.assertEqual(
            payload["generationConfig"]["responseSchema"],
            {
                "type": "OBJECT",
                "properties": {
                    "reply": {"type": "STRING"},
                    "is_coding_request": {"type": "BOOLEAN"},
                },
                "required": ["reply", "is_coding_request"],
            },
        )

    def test_gemini_provider_requires_api_key(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"PHONE_MODEL_PROVIDER": "gemini", "GEMINI_API_KEY": ""},
            ),
            self.assertRaisesRegex(phone_model.PhoneModelError, "GEMINI_API_KEY"),
        ):
            phone_model.chat([{"role": "user", "content": "Hi"}])

    def test_gemini_provider_defaults_to_gemini_38_flash(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Hi"}]}}]
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with (
            patch.dict(
                os.environ,
                {
                    "PHONE_MODEL_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "cloud-key",
                    "PHONE_MODEL_TIMEOUT_SECONDS": "120",
                },
                clear=True,
            ),
            patch("Ai.phone_model.httpx.Client", return_value=client),
        ):
            phone_model.chat([{"role": "user", "content": "Hi"}])

        self.assertIn("/gemini-3.8-flash:generateContent", client.post.call_args.args[0])

    def test_unknown_provider_is_rejected(self) -> None:
        with (
            patch.dict(os.environ, {"PHONE_MODEL_PROVIDER": "unknown"}),
            self.assertRaisesRegex(phone_model.PhoneModelError, "PHONE_MODEL_PROVIDER"),
        ):
            phone_model.chat([{"role": "user", "content": "Hi"}])

    def test_missing_base_url_has_actionable_error(self) -> None:
        os.environ.pop("PHONE_MODEL_BASE_URL")

        with self.assertRaisesRegex(
            phone_model.PhoneModelError,
            "Set PHONE_MODEL_BASE_URL",
        ):
            phone_model.chat([{"role": "user", "content": "Hi"}])

    def test_unauthorized_response_does_not_expose_key(self) -> None:
        response = Mock()
        response.status_code = 401
        response.is_success = False
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with patch("Ai.phone_model.httpx.Client", return_value=client):
            with self.assertRaisesRegex(
                phone_model.PhoneModelError,
                "check TEST_PHONE_API_KEY",
            ) as error:
                phone_model.chat([{"role": "user", "content": "Hi"}])

        self.assertNotIn("test-key", str(error.exception))

    def test_empty_visible_content_reports_finish_reason_without_returning_reasoning(self) -> None:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "reasoning_content": "private model reasoning",
                    },
                }
            ]
        }
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response

        with patch("Ai.phone_model.httpx.Client", return_value=client):
            with self.assertRaisesRegex(
                phone_model.PhoneModelError,
                "finish_reason=length.*reasoning characters but no visible content",
            ):
                phone_model.chat([{"role": "user", "content": "Hi"}])


class PhoneModelTlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "PHONE_MODEL_BASE_URL": "https://192.168.1.42:8080/v1",
                "TEST_PHONE_API_KEY": "test-key",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def verify_argument(self) -> object:
        response = Mock()
        response.status_code = 200
        response.is_success = True
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        with patch("Ai.phone_model.httpx.Client", return_value=client) as factory:
            phone_model.chat([{"role": "user", "content": "Hi"}])
        return factory.call_args.kwargs["verify"]

    def test_certificates_are_verified_by_default(self) -> None:
        self.assertIs(self.verify_argument(), True)

    def test_a_private_ca_or_pinned_certificate_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            material = tls.prepare_server_tls(directory=Path(directory))
            os.environ["PHONE_MODEL_CA_CERT"] = str(material.certfile)

            verify = self.verify_argument()

        self.assertIsInstance(verify, ssl.SSLContext)
        self.assertEqual(verify.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(verify.check_hostname)

    def test_an_unreadable_ca_file_is_reported_without_sending_anything(self) -> None:
        os.environ["PHONE_MODEL_CA_CERT"] = "/nonexistent/phone-ca.pem"

        with patch("Ai.phone_model.httpx.Client") as factory:
            with self.assertRaisesRegex(phone_model.PhoneModelError, "PHONE_MODEL_CA_CERT"):
                phone_model.chat([{"role": "user", "content": "Hi"}])

        factory.return_value.__enter__.return_value.post.assert_not_called()

    def test_plain_http_to_another_machine_is_flagged(self) -> None:
        cases = {
            "http://192.168.1.42:8080/v1": True,
            "http://phone.local:8080/v1": True,
            "http://localhost:8080/v1": False,
            "http://127.0.0.1:8080/v1": False,
            "http://[::1]:8080/v1": False,
            "https://192.168.1.42:8080/v1": False,
            "": False,
        }
        for url, flagged in cases.items():
            with self.subTest(url=url):
                os.environ["PHONE_MODEL_BASE_URL"] = url
                warning = phone_model.plaintext_link_warning()
                self.assertEqual(warning is not None, flagged)
                if flagged:
                    self.assertIn("unencrypted", warning)


if __name__ == "__main__":
    unittest.main()
