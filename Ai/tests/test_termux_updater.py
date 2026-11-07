"""Exercise signed upgrades with the real HTTPS server and an offline model stub."""

import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest


AI = Path(__file__).resolve().parents[1]
UPDATER = AI / "termux" / "update-server.py"
RELEASE = "super-bot-release.tar.gz"


class ModelStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data":[{"id":"superbot-phone"}]}'
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(shutil.which("openssl") and os.name == "posix", "Requires POSIX and OpenSSL")
class TermuxUpdaterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.install = self.temp / "install"
        self.install.mkdir()
        self.publish = self.temp / "publish"
        self.publish.mkdir()
        self.data = self.install / "server-data"
        self.data.mkdir()
        self.private_key = self.temp / "private.pem"
        self.public_key = self.temp / "public.pem"
        self.openssl("genpkey", "-algorithm", "RSA", "-out", self.private_key)
        self.openssl("pkey", "-in", self.private_key, "-pubout", "-out", self.public_key)

        model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
        self.addCleanup(model.server_close)
        worker = threading.Thread(target=model.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(model.shutdown)
        self.port = free_port(socket.SOCK_STREAM)
        self.env = {
            **os.environ,
            "HOST": "127.0.0.1",
            "DISCOVERY_PORT": str(free_port(socket.SOCK_DGRAM)),
            "SUPER_BOT_SERVER_DIR": str(self.data),
            "PHONE_MODEL_PROVIDER": "phone",
            "PHONE_MODEL_BASE_URL": f"http://127.0.0.1:{model.server_port}/v1",
            "PHONE_MODEL_NAME": "superbot-phone",
            "TEST_PHONE_API_KEY": "test-key",
            "SSL_CERTFILE": "",
            "SSL_KEYFILE": "",
        }
        self.pointer = self.install / ".releases" / "active"

    def openssl(self, *args):
        subprocess.run(["openssl", *args], check=True, capture_output=True)

    def publish_release(self, gateway_suffix="", *, tamper=False, extra_member=None):
        archive = self.publish / RELEASE
        with tarfile.open(archive, "w:gz") as bundle:
            for path in sorted([*AI.glob("*.py"), AI / "requirements.txt"]):
                content = path.read_bytes()
                if path.name == "gateway.py":
                    content += gateway_suffix.encode()
                member = tarfile.TarInfo(f"Ai/{path.name}")
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            if extra_member:
                member = tarfile.TarInfo(extra_member)
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
        self.openssl("dgst", "-sha256", "-sign", self.private_key,
                     "-out", self.publish / f"{RELEASE}.sig", archive)
        if tamper:
            with archive.open("ab") as content:
                content.write(b"tampered")
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    @contextlib.contextmanager
    def start(self):
        with tempfile.TemporaryFile(mode="w+") as output:
            process = subprocess.Popen(
                [sys.executable, str(UPDATER), "--install", str(self.install),
                 "--url", self.publish.as_uri(), "--key", str(self.public_key),
                 "--port", str(self.port), "--startup-timeout", "5"],
                env=self.env, stdout=output, stderr=subprocess.STDOUT,
            )
            try:
                yield process, output
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    self.fail("Updater did not stop its server gracefully")

    def wait_for_build(self, process, output, build, message):
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            connection = http.client.HTTPSConnection(
                "127.0.0.1", self.port, timeout=0.5,
                context=ssl._create_unverified_context(),
            )
            try:
                connection.request("GET", "/health")
                health = json.loads(connection.getresponse().read())
                output.seek(0)
                if health.get("build") == build and message in output.read():
                    return health["startup_id"]
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        output.seek(0)
        self.fail(output.read())

    def test_signed_upgrade_and_wrong_build_rollback_preserve_data(self):
        first = self.publish_release()
        with self.start() as (process, output):
            self.wait_for_build(process, output, first, "Update verified and activated")
        second = self.publish_release("\n# Second signed release\n")
        with self.start() as (process, output):
            startup = self.wait_for_build(process, output, second, "Update verified and activated")
            self.assertEqual(self.pointer.read_text().strip(), second)

        db_path = self.install / "chats.sqlite"
        with contextlib.closing(sqlite3.connect(db_path)) as database, database:
            database.execute("CREATE TABLE chats (message TEXT)")
            database.execute("INSERT INTO chats VALUES ('user-owned history')")
        config = self.install / ".env"
        config.write_text("# preserve this configuration\n")
        original_files = {path: path.read_bytes() for path in [*self.data.iterdir(), config, db_path]
                          if path.is_file()}
        # A legitimate signature and HTTP 200 are insufficient when the
        # gateway cannot identify the running build.
        candidate = self.publish_release(
            "\n_original_gateway = build_gateway\n"
            "def build_gateway(*args, **kwargs):\n"
            "    app = _original_gateway(*args, **kwargs)\n"
            "    app.router.routes[:] = [route for route in app.routes if route.path != '/health']\n"
            "    app.get('/health')(lambda: {'status': 'ok'})\n"
            "    return app\n"
        )
        self.assertNotEqual(candidate, second)
        with self.start() as (process, output):
            restored = self.wait_for_build(process, output, second, "Previous release verified")
            self.assertNotEqual(restored, startup)
            self.assertEqual(self.pointer.read_text().strip(), second)
            output.seek(0)
            log = output.read()
            self.assertIn(f"Build {candidate} did not pass startup verification", log)
            self.assertNotIn(f"Update verified and activated: {candidate}", log)
            self.assertEqual({path: path.read_bytes() for path in original_files}, original_files)

    def test_rejects_tampered_release_and_signed_path_traversal(self):
        for options, expected in [
            ({"tamper": True}, "Update rejected"),
            ({"extra_member": "Ai/../../outside"}, "Invalid release member"),
        ]:
            with self.subTest(options=options):
                self.publish_release(**options)
                with self.start() as (process, output):
                    self.assertEqual(process.wait(timeout=10), 1)
                    output.seek(0)
                    self.assertIn(expected, output.read())
                    self.assertFalse(self.pointer.exists())
                    self.assertFalse((self.install / ".releases" / "outside").exists())
