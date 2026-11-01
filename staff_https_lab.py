"""HTTPS preview restricted to disposable synthetic data and loopback.

Run explicitly with ``python -B -m staff_https_lab --cert ... --key ...``.
No real database/model path, bind address, proxy, or write mode is configurable.
Certificate trust must be established by the developer outside this command.
"""

import argparse
from contextlib import contextmanager
from datetime import timedelta
import errno
from getpass import getpass
from hashlib import sha256
import os
from pathlib import Path
import ssl
import socket
import stat
import sys

import uvicorn

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import PROJECT, temporary_workspace
from reception_migration.synthetic import BASE, create_v1


DIAGNOSTICS = {
    "tls_files": "TLS files are unavailable; check the supplied files and directory access.",
    "tls_path": "TLS files must be regular, single-link files outside the repository.",
    "tls_owner": "TLS files must be owned by the current WSL user.",
    "tls_key_permissions": "The TLS private key must be accessible only to its owner (mode 0600).",
    "tls_pair": "The TLS certificate/key could not be loaded as a matching pair; use an unencrypted development key.",
    "credential_missing": "No development credential was provided.",
    "credential_length": "The development credential must contain at least 32 characters.",
    "credential_ascii": "The development credential must contain ASCII characters only.",
    "credential_printable": "The development credential must not contain control characters.",
    "credential_blank": "The development credential must not consist only of spaces.",
    "credential_terminal": "A terminal with hidden input is required, or configure OMACHI_LAB_STAFF_TOKEN locally.",
    "credential_input": "Hidden credential input ended before a value was received.",
    "port_range": "Choose a port between 1024 and 65535.",
    "port_in_use": "The selected 127.0.0.1 port is already in use; stop its owner or choose --port 8444.",
    "port_permission": "Permission to bind the selected loopback port was denied.",
    "port_unavailable": "The selected loopback port could not be checked.",
    "synthetic_database": "Disposable synthetic DB preparation failed; check temporary-directory access and free space.",
    "server_startup": "The TLS server could not start; no HTTP fallback was attempted.",
    "configuration": "The development preview configuration is invalid.",
}


class LabConfigurationError(ValueError):
    """Only allowlisted codes may cross the diagnostic boundary."""

    def __init__(self, code):
        self.code = code if code in DIAGNOSTICS else "configuration"
        super().__init__(self.code)


def validate_tls(cert, key):
    """Check supplied files without generating keys or modifying trust stores."""
    checked = []
    for value, private in ((cert, False), (key, True)):
        path = Path(value).expanduser().absolute()
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or resolved.is_relative_to(PROJECT)):
                raise LabConfigurationError("tls_path")
            if info.st_uid != os.getuid():
                raise LabConfigurationError("tls_owner")
            if private and stat.S_IMODE(info.st_mode) & 0o077:
                raise LabConfigurationError("tls_key_permissions")
        except OSError as exc:
            raise LabConfigurationError("tls_files") from exc
        checked.append(str(resolved))
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(*checked, password=lambda: "")
    except (OSError, ssl.SSLError) as exc:
        raise LabConfigurationError("tls_pair") from exc
    return tuple(checked)


def validate_credential(token):
    # Preserve the existing input policy; never return/log the value or its size.
    if not isinstance(token, str):
        raise LabConfigurationError("credential_missing")
    if len(token) < 32:
        raise LabConfigurationError("credential_length")
    if not token.isascii():
        raise LabConfigurationError("credential_ascii")
    if any(not char.isprintable() for char in token):
        raise LabConfigurationError("credential_printable")
    if not token.strip():
        raise LabConfigurationError("credential_blank")


def validate_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LabConfigurationError("port_range")


def check_loopback_port(port):
    """Bind only loopback, briefly, without listening or modifying any service.

    A later bind race can still fail at server startup; never pick another port
    or widen the listen address automatically.
    """
    validate_port(port)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Match asyncio/Uvicorn on POSIX: a recently closed connection must
            # not look like a live listener merely because it is in TIME_WAIT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        code = {errno.EADDRINUSE: "port_in_use", errno.EACCES: "port_permission",
                errno.EPERM: "port_permission"}.get(exc.errno, "port_unavailable")
        raise LabConfigurationError(code) from exc


class ReadOnlyLab:
    """Additional preview boundary: only staff reads and session lifecycle."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path, method = scope.get("path", ""), scope.get("method", "")
            allowed = path.startswith("/staff/") and (
                method in ("GET", "HEAD")
                or path == "/staff/session" and method in ("POST", "DELETE")
            )
            if not allowed:
                from starlette.responses import JSONResponse

                response = JSONResponse({"detail": "合成データの閲覧専用環境です。"}, status_code=403,
                                        headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@contextmanager
def synthetic_app(token, port):
    """Create and migrate a newly generated temporary DB, never an input DB."""
    validate_credential(token)
    validate_port(port)
    with temporary_workspace() as workspace:
        database, _ = create_v1(workspace)
        migrate(workspace, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        # Use the fixture's final timestamp so recent seating remains visible.
        # Monotonic session expiry is unaffected; there is no live business clock.
        frozen_time = BASE + timedelta(minutes=2)
        app = create_app(
            db_path=database, model_path=workspace.root / "no-model-in-this-synthetic-lab.joblib",
            staff_token=token, staff_browser_origin=f"https://staff.localhost:{port}",
            clock=lambda: frozen_time,
        )
        yield ReadOnlyLab(app)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Localhost HTTPS staff UI preview; disposable synthetic DB only.")
    parser.add_argument("--cert", required=True, help="Existing development TLS certificate outside this repository")
    parser.add_argument("--key", required=True, help="Existing private TLS key outside this repository (mode 0600)")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--check", action="store_true", help="Validate TLS, loopback port, hidden credential and synthetic DB, then exit without starting the server")
    args = parser.parse_args(argv)
    stage = "tls_files"
    try:
        cert, key = validate_tls(args.cert, args.key)
        stage = "port_unavailable"
        check_loopback_port(args.port)
        stage = "credential_input"
        token = os.environ.get("OMACHI_LAB_STAFF_TOKEN")
        if token is None:
            if not sys.stdin.isatty():
                raise LabConfigurationError("credential_terminal")
            token = getpass("Development shared staff credential (32+ printable ASCII characters; hidden): ")
        validate_credential(token)
        stage = "synthetic_database"
        with synthetic_app(token, args.port) as app:
            if args.check:
                print("[check_ok] TLS pair, file policy, loopback port, credential rules and disposable synthetic DB passed; no server started.", flush=True)
                return 0
            print(f"Synthetic data only: https://staff.localhost:{args.port}/staff/login", flush=True)
            stage = "server_startup"
            uvicorn.run(
                app, host="127.0.0.1", port=args.port, workers=1, reload=False,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                log_level="warning", ssl_certfile=cert, ssl_keyfile=key,
                ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER,
            )
            stage = "synthetic_database"  # Context cleanup can fail independently of serving.
    except Exception as exc:
        # Do not echo arbitrary exception text, paths, credentials, key material,
        # or tracebacks. Known errors have actionable, fixed messages.
        code = exc.code if isinstance(exc, LabConfigurationError) else stage
        parser.exit(2, f"The HTTPS preview could not start. [{code}] {DIAGNOSTICS[code]}\n")
    except SystemExit as exc:
        # Uvicorn reports bind/lifespan failures via SystemExit, not OSError.
        if stage != "server_startup" or exc.code in (None, 0):
            raise
        parser.exit(2, f"The HTTPS preview could not start. [server_startup] {DIAGNOSTICS['server_startup']}\n")
    except KeyboardInterrupt:
        parser.exit(130, "HTTPS preview cancelled.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
