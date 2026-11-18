"""A disposable HTTPS preview can never select a real database or write API."""

from pathlib import Path
from contextlib import contextmanager
from http.client import HTTPResponse
from http.cookies import SimpleCookie
import json
import errno
import os
import select
import signal
import shutil
import socket
import ssl
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep

from fastapi.testclient import TestClient
import pytest

import staff_https_lab as lab


TOKEN = "synthetic-lab-credential-only-12345678901234567890"
ORIGIN = "https://staff.localhost:8443"


@pytest.fixture
def certificate(tmp_path):
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("OpenSSL is needed to generate disposable test keys.")
    cert, key = tmp_path / "synthetic-cert.pem", tmp_path / "synthetic-key.pem"
    subprocess.run(
        [executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=staff.localhost", "-addext", "subjectAltName=DNS:staff.localhost",
         "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


def test_tls_requires_matching_external_private_files(certificate, tmp_path):
    cert, key = certificate
    assert lab.validate_tls(cert, key) == (str(cert), str(key))
    key.chmod(0o644)
    with pytest.raises(lab.LabConfigurationError):
        lab.validate_tls(cert, key)
    key.chmod(0o600)
    with pytest.raises(lab.LabConfigurationError):
        lab.validate_tls(Path(lab.__file__), key)
    with pytest.raises(lab.LabConfigurationError):
        lab.validate_tls(cert, tmp_path / "missing.pem")
    other = tmp_path / "invalid-key.pem"
    other.write_text("not a private key")
    other.chmod(0o600)
    with pytest.raises(lab.LabConfigurationError):
        lab.validate_tls(cert, other)


@pytest.mark.parametrize("link_type", ["symbolic", "hard"])
def test_tls_aliases_are_rejected(certificate, tmp_path, link_type):
    cert, key = certificate
    alias = tmp_path / "alias-key.pem"
    if link_type == "symbolic":
        alias.symlink_to(key)
    else:
        alias.hardlink_to(key)
    with pytest.raises(lab.LabConfigurationError):
        lab.validate_tls(cert, alias)


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31, "x" * 40 + "\n", "あ" * 40])
def test_synthetic_app_rejects_unusable_credentials(token):
    with pytest.raises(lab.LabConfigurationError):
        with lab.synthetic_app(token, 8443):
            pytest.fail("Unsafe credentials must fail before opening an app.")


@pytest.mark.parametrize("port", [0, 443, 65536, "8443", True])
def test_synthetic_app_has_only_explicit_unprivileged_ports(port):
    with pytest.raises(lab.LabConfigurationError):
        with lab.synthetic_app(TOKEN, port):
            pytest.fail("Invalid port accepted.")


def test_synthetic_preview_is_disposable_readonly_v2_legacy(monkeypatch):
    # Even hostile inherited production settings cannot select a database/model.
    monkeypatch.setenv("OMACHI_DB_PATH", "/must-not-open-production.sqlite3")
    monkeypatch.setenv("OMACHI_MODEL_PATH", "/must-not-load-model.joblib")
    with lab.synthetic_app(TOKEN, 8443) as app:
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50001)) as client:
            database = app.app.state.store.db_path
            assert database.parent.parent == Path("/tmp")
            before = database.read_bytes()
            assert not app.app.state.predictor.available
            login = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
            assert login.status_code == 200
            assert login.headers["x-omachi-preview"] == "synthetic"
            for flag in ("Secure", "HttpOnly", "SameSite=strict"):
                assert flag in login.headers["set-cookie"]
            active = client.get("/staff/receptions").json()
            assert len(active["receptions"]) == 3
            assert active["metadata"]["schema_version"] == 2
            assert active["metadata"]["staff_write_mode"] == "legacy"
            assert active["metadata"]["seat_corrections_enabled"] == 0
            assert active["metadata"]["write_enabled"] is False
            recent = client.get("/staff/receptions?view=recently_seated").json()
            assert len(recent["receptions"]) == 3
            for row in active["receptions"] + recent["receptions"]:
                assert row["allowed_actions"] == []
                assert client.get(f'/staff/receptions/{row["id"]}/operation-context').status_code == 200
            csrf = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
            receipt = active["receptions"][0]["id"]
            for method, url in (("post", "/receptions"), ("get", "/"),
                                ("post", f"/staff/receptions/{receipt}/operations"),
                                ("post", f"/staff/receptions/{receipt}/seating-corrections"),
                                ("post", f"/staff/receptions/{receipt}/quality-reviews")):
                assert getattr(client, method)(url, headers=csrf).status_code == 403
            assert client.delete("/staff/session", headers=csrf).status_code == 200
            assert client.get("/staff/receptions").status_code == 401
            assert database.read_bytes() == before
        root = database.parent
        assert root.exists()
    assert not root.exists()


def test_main_fixes_loopback_tls_single_process_and_never_prints_credential(certificate, monkeypatch, capsys):
    cert, key = certificate
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", TOKEN)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)
    calls = []
    monkeypatch.setattr(lab.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert lab.main(["--cert", str(cert), "--key", str(key)]) == 0
    assert len(calls) == 1
    app, options = calls[0]
    assert isinstance(app, lab.ReadOnlyLab)
    assert options == dict(host="127.0.0.1", port=8443, workers=1, reload=False,
                           proxy_headers=False, forwarded_allow_ips="", access_log=False,
                           log_level="warning", ssl_certfile=str(cert), ssl_keyfile=str(key),
                           ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER)
    output = capsys.readouterr()
    assert "https://staff.localhost:8443/staff/login" in output.out
    assert TOKEN not in output.out + output.err


def test_main_no_insecure_fallback_or_echo_without_terminal(certificate, monkeypatch, capsys):
    cert, key = certificate
    monkeypatch.delenv("OMACHI_LAB_STAFF_TOKEN", raising=False)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)
    monkeypatch.setattr(lab.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(lab.uvicorn, "run", lambda *args, **kwargs: pytest.fail("Server must remain closed."))
    monkeypatch.setattr(lab, "getpass", lambda *args, **kwargs: pytest.fail("No echoing fallback allowed."))
    with pytest.raises(SystemExit) as error:
        lab.main(["--cert", str(cert), "--key", str(key)])
    assert error.value.code == 2
    assert "could not start" in capsys.readouterr().err


def test_main_rejects_host_database_and_model_arguments():
    with pytest.raises(SystemExit) as error:
        lab.main(["--cert", "unused", "--key", "unused", "--host", "0.0.0.0",
                  "--database", "var/receptions.sqlite3", "--model", "models/real.joblib"])
    assert error.value.code == 2


def test_real_loopback_tls_cookie_origin_csrf_and_logout(certificate):
    """Verify actual TLS transport, without installing or bypassing a root CA."""
    cert, key = certificate
    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    listening.listen(16)
    port = listening.getsockname()[1]
    origin = f"https://staff.localhost:{port}"
    client_context = ssl.create_default_context(cafile=str(cert))

    def request(method, path, headers=None):
        # Pin the connection to loopback, but verify the real staff certificate
        # hostname and send staff.localhost SNI. No resolver/hosts change needed.
        with socket.create_connection(("127.0.0.1", port), timeout=3) as plain:
            with client_context.wrap_socket(plain, server_hostname="staff.localhost") as stream:
                values = {"Host": f"staff.localhost:{port}", "Connection": "close", "Content-Length": "0"}
                values.update(headers or {})
                message = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in values.items()) + "\r\n"
                stream.sendall(message.encode("ascii"))
                response = HTTPResponse(stream)
                response.begin()
                return response.status, dict(response.getheaders()), response.read()

    try:
        with lab.synthetic_app(TOKEN, port) as app:
            config = lab.uvicorn.Config(app, host="127.0.0.1", port=port, workers=1,
                                       proxy_headers=False, forwarded_allow_ips="", access_log=False,
                                       log_level="error", ssl_certfile=str(cert), ssl_keyfile=str(key),
                                       ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER)
            server = lab.uvicorn.Server(config)
            thread = Thread(target=server.run, kwargs={"sockets": [listening]}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 10
                while not server.started and thread.is_alive() and monotonic() < deadline:
                    sleep(0.01)
                assert server.started, "Synthetic TLS server did not start."
                status, _, _ = request("GET", "/staff/receptions")
                assert status == 401
                status, headers, body = request("POST", "/staff/session", {"Origin": origin, "X-Staff-Token": TOKEN})
                assert status == 200
                cookie = SimpleCookie()
                cookie.load(headers["set-cookie"])
                morsel = cookie["__Host-omachi-staff"]
                assert morsel["secure"] and morsel["httponly"] and morsel["samesite"] == "strict"
                cookie_header = morsel.OutputString(attrs=[])
                csrf = json.loads(body)["csrf_token"]
                auth = {"Cookie": cookie_header}
                status, _, body = request("GET", "/staff/receptions", auth)
                assert status == 200 and len(json.loads(body)["receptions"]) == 3
                assert request("GET", "/staff/receptions", {**auth, "Origin": "https://localhost"})[0] == 403
                assert request("DELETE", "/staff/session", {**auth, "Origin": origin})[0] == 403
                assert request("DELETE", "/staff/session", {**auth, "Origin": origin, "X-CSRF-Token": csrf})[0] == 200
                assert request("GET", "/staff/receptions", auth)[0] == 401
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive(), "Synthetic TLS server must stop."
    finally:
        listening.close()


@pytest.mark.parametrize("value,code", [
    (None, "credential_missing"), ("x" * 31, "credential_length"),
    ("あ" * 32, "credential_ascii"), ("x" * 32 + "\n", "credential_printable"),
    ("x" * 32 + "\t", "credential_printable"), (" " * 32, "credential_blank"),
])
def test_credential_diagnostics_preserve_rules_without_echo(value, code):
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.validate_credential(value)
    assert failure.value.code == code
    assert str(failure.value) == code


@pytest.mark.parametrize("value", ["x" * 32, "x" * 64, "A1! " * 8])
def test_credential_minimum_and_printable_ascii_are_unchanged(value):
    assert lab.validate_credential(value) is None


@pytest.mark.parametrize("value,code", [("x" * 31, "credential_length"),
    ("あ" * 32, "credential_ascii"), (EOFError("sensitive input failure"), "credential_input")])
def test_interactive_failure_after_tls_has_specific_safe_diagnostic(certificate, monkeypatch, capsys, value, code):
    cert, key = certificate
    monkeypatch.delenv("OMACHI_LAB_STAFF_TOKEN", raising=False)
    monkeypatch.setattr(lab.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)

    def hidden_input(prompt):
        assert "32+ printable ASCII" in prompt
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(lab, "getpass", hidden_input)
    monkeypatch.setattr(lab.uvicorn, "run", lambda *args, **kwargs: pytest.fail("Invalid input must not start TLS."))
    with pytest.raises(SystemExit) as failure:
        lab.main(["--cert", str(cert), "--key", str(key)])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert f"[{code}]" in output.err
    assert "Synthetic data only:" not in output.out
    for secret in (str(value), str(cert), str(key), "sensitive input failure", "Traceback"):
        assert secret not in output.out + output.err


def test_interactive_valid_minimum_reaches_server_without_echo(certificate, monkeypatch, capsys):
    cert, key = certificate
    value = "A1!x" * 8
    monkeypatch.delenv("OMACHI_LAB_STAFF_TOKEN", raising=False)
    monkeypatch.setattr(lab.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(lab, "getpass", lambda prompt: value)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)
    calls = []
    monkeypatch.setattr(lab.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    assert lab.main(["--cert", str(cert), "--key", str(key)]) == 0
    assert len(calls) == 1 and calls[0]["host"] == "127.0.0.1"
    output = capsys.readouterr()
    assert value not in output.out + output.err


def test_tls_file_diagnostics_check_permissions_owner_and_missing(certificate, monkeypatch, tmp_path):
    cert, key = certificate
    key.chmod(0o640)
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.validate_tls(cert, key)
    assert failure.value.code == "tls_key_permissions"
    key.chmod(0o600)
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.validate_tls(tmp_path / "not-there.pem", key)
    assert failure.value.code == "tls_files"
    owner = cert.stat().st_uid
    monkeypatch.setattr(lab.os, "getuid", lambda: owner + 1)
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.validate_tls(cert, key)
    assert failure.value.code == "tls_owner"


def test_mismatched_valid_certificate_key_pair_has_safe_code(certificate, tmp_path):
    cert, _ = certificate
    key = tmp_path / "other-key.pem"
    subprocess.run([shutil.which("openssl"), "genpkey", "-algorithm", "RSA",
                    "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)], check=True, capture_output=True)
    key.chmod(0o600)
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.validate_tls(cert, key)
    assert failure.value.code == "tls_pair"


def test_loopback_port_conflict_is_detected_without_stopping_owner():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen(1)
        port = owner.getsockname()[1]
        with pytest.raises(lab.LabConfigurationError) as failure:
            lab.check_loopback_port(port)
        assert failure.value.code == "port_in_use"
        assert owner.getsockname()[1] == port
    lab.check_loopback_port(port)  # Probe releases the socket immediately.


def test_loopback_probe_allows_restart_after_a_closed_connection():
    # An actual live listener remains forbidden, but a server-side TIME_WAIT
    # must not prevent a normal Uvicorn restart on POSIX.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                connection.shutdown(socket.SHUT_WR)
                assert peer.recv(1) == b""
                peer.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
    lab.check_loopback_port(port)


@pytest.mark.parametrize("number,code", [(errno.EACCES, "port_permission"),
                                       (errno.EPERM, "port_permission"), (errno.EIO, "port_unavailable")])
def test_loopback_probe_failures_are_fixed_codes(monkeypatch, number, code):
    def fail(*args):
        raise OSError(number, "sensitive OS error")
    monkeypatch.setattr(lab.socket, "socket", fail)
    with pytest.raises(lab.LabConfigurationError) as failure:
        lab.check_loopback_port(8443)
    assert failure.value.code == code and "sensitive" not in str(failure.value)


def test_check_mode_prepares_only_disposable_database_and_never_starts_server(certificate, monkeypatch, capsys):
    cert, key = certificate
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", TOKEN)
    ports = []
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: ports.append(port))
    monkeypatch.setattr(lab.uvicorn, "run", lambda *args, **kwargs: pytest.fail("Check mode must not start a server."))
    real_app = lab.synthetic_app
    roots = []

    @contextmanager
    def observe(*args):
        with real_app(*args) as app:
            # No lifespan or existing DB is opened by --check.
            roots.append(set(Path("/tmp").glob("omachi-migration-*")))
            yield app

    monkeypatch.setattr(lab, "synthetic_app", observe)
    before = set(Path("/tmp").glob("omachi-migration-*"))
    assert lab.main(["--cert", str(cert), "--key", str(key), "--check"]) == 0
    assert ports == [8443] and roots[0] - before
    assert not (set(Path("/tmp").glob("omachi-migration-*")) - before)
    output = capsys.readouterr()
    assert "[check_ok]" in output.out and "no server started" in output.out
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize("stage,code", [("tls", "tls_pair"), ("port", "port_in_use"),
    ("database", "synthetic_database"), ("server", "server_startup"), ("server_exit", "server_startup")])
def test_main_reports_stage_without_arbitrary_exception_text(certificate, monkeypatch, capsys, stage, code):
    cert, key = certificate
    marker = "synthetic-sensitive-do-not-echo"
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", TOKEN)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)

    def fail(*args, **kwargs):
        if stage in ("tls", "port"):
            raise lab.LabConfigurationError(code)
        if stage == "server_exit":
            raise SystemExit(1)
        raise OSError(marker)

    if stage == "tls":
        monkeypatch.setattr(lab, "validate_tls", fail)
    elif stage == "port":
        monkeypatch.setattr(lab, "check_loopback_port", fail)
    elif stage == "database":
        monkeypatch.setattr(lab, "synthetic_app", fail)
    else:
        monkeypatch.setattr(lab.uvicorn, "run", fail)
    with pytest.raises(SystemExit) as failure:
        lab.main(["--cert", str(cert), "--key", str(key)])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert f"[{code}]" in output.err
    assert all(value not in output.out + output.err for value in (marker, TOKEN, str(cert), str(key), "Traceback"))


def test_normal_cli_reaches_verified_tls_with_synthetic_credential(certificate):
    cert, key = certificate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"https://staff.localhost:{port}"
    context = ssl.create_default_context(cafile=str(cert))
    environment = {**os.environ, "OMACHI_LAB_STAFF_TOKEN": TOKEN}
    process = subprocess.Popen([sys.executable, "-B", "-m", "staff_https_lab", "--cert", str(cert),
                                "--key", str(key), "--port", str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 15)
        assert ready, "Synthetic CLI did not report its startup stage."
        assert origin.encode() in process.stdout.readline()
        deadline = monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as plain:
                    with context.wrap_socket(plain, server_hostname="staff.localhost") as stream:
                        message = (f"POST /staff/session HTTP/1.1\r\nHost: staff.localhost:{port}\r\n"
                                   f"Origin: {origin}\r\nX-Staff-Token: {TOKEN}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                        stream.sendall(message.encode("ascii"))
                        response = HTTPResponse(stream)
                        response.begin()
                        assert response.status == 200
                        assert json.loads(response.read())["authenticated"] is True
                        assert "Secure" in response.getheader("Set-Cookie")
                        break
            except ConnectionRefusedError:
                assert process.poll() is None and monotonic() < deadline
                sleep(0.02)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise AssertionError("Synthetic CLI must shut down.")
    assert process.returncode == 0
    assert TOKEN.encode() not in stdout + stderr
    assert b"could not start" not in stderr and b"Traceback" not in stderr
