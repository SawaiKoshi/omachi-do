"""Explicit call-only HTTPS experiment on a newly generated disposable DB.

The ordinary staff_https_lab remains read-only. This entry point has no DB,
model, bind-address, worker-count or general write-enable option. The staff UI
remains read-only; callers must use the existing authenticated operations API.
"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from getpass import getpass
from hashlib import sha256
import json
import os
import re
import ssl
import sys
from uuid import uuid4

from pydantic import ValidationError
from starlette.responses import JSONResponse
import uvicorn

from prediction_api import StaffOperationInput, create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import BASE, create_v1
from reception_store import ReceptionStore
from staff_https_lab import (
    DIAGNOSTICS, LabConfigurationError, ReadOnlyLab, check_loopback_port,
    validate_credential, validate_port, validate_tls,
)


MAX_BODY_BYTES = 4096
BODY_TIMEOUT_SECONDS = 5
OPERATIONS_PATH = re.compile(r"/staff/receptions/[0-9a-fA-F-]{36}/operations\Z")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("Non-JSON number")


class CallOnlyLab:
    """Narrow transport gate; authentication and transitions stay in the app.

    The gate forwards only validated call JSON, never performs a store write,
    and never forwards an alternate action even if the client is authenticated.
    """

    def __init__(self, app, port):
        validate_port(port)
        self.app = app
        self.readonly = ReadOnlyLab(app)
        self.authority = f"staff.localhost:{port}".encode("ascii")

    async def __call__(self, scope, receive, send):
        async def preview_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                if not any(name.lower() == b"x-omachi-preview" for name, _ in headers):
                    headers.append((b"x-omachi-preview", b"synthetic"))
                headers.append((b"x-omachi-preview-actions", b"call"))
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status=403):
            # Never echo request bodies, validation exceptions or credentials.
            await JSONResponse({"detail": "Call-only synthetic HTTPS preview request rejected."},
                               status_code=status, headers={"Cache-Control": "no-store"})(
                                   scope, receive, preview_send)

        if scope["type"] != "http":
            await self.readonly(scope, receive, preview_send)
            return
        headers = scope.get("headers", ())

        def values(name):
            return [value for key, value in headers if key.lower() == name]

        # Do not let a localhost/native-token route bypass browser session auth.
        if scope.get("scheme") != "https" or values(b"host") != [self.authority]:
            await reject()
            return
        path = scope.get("path", "")
        if scope["method"] != "POST" or not OPERATIONS_PATH.fullmatch(path):
            await self.readonly(scope, receive, preview_send)
            return
        content_types = values(b"content-type")
        if (scope.get("query_string") or len(content_types) != 1
                or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json"
                or values(b"content-encoding")):
            await reject(422)
            return
        lengths = values(b"content-length")
        if lengths and (len(lengths) != 1 or not lengths[0].isdigit()):
            await reject(422)
            return
        if lengths and (len(lengths[0]) > 5 or int(lengths[0]) > MAX_BODY_BYTES):
            await reject(413)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return  # Disconnect before validation: nothing forwarded.
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY_BYTES:
                        await reject(413)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408)
            return
        try:
            if lengths and int(lengths[0]) != len(body):
                raise ValueError("Body length mismatch")
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                 parse_constant=_invalid_constant)
            command = StaffOperationInput.model_validate(payload)
        except (ValueError, ValidationError, RecursionError):
            await reject(422)
            return
        if command.action != "call":
            await reject()
            return

        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        # Original, validated bytes go through the existing Cookie/Origin/CSRF,
        # protected route and atomic store operation. No alternate API fallback.
        await self.app(scope, replay_receive, preview_send)


@contextmanager
def synthetic_app(token, port):
    validate_credential(token)
    validate_port(port)
    with temporary_workspace() as workspace:
        # The migration stress fixture intentionally contains arbitrary legacy
        # JSON (not an API response contract). Generate this lab's rows through
        # the existing v1 store instead; never repair/overwrite saved predictions.
        database, _ = create_v1(workspace, populated=False)
        stamp = [BASE]
        seed = ReceptionStore(database, clock=lambda: stamp[0], expected_schema_version=1)
        rows = {}
        for status, adults, children in (("waiting", 1, 1), ("called", 3, 1), ("absent", 4, 1),
                                         ("seated1", 2, 0), ("seated2", 3, 0), ("seated3", 4, 0)):
            record, _ = seed.register(adults, children, str(uuid4()))
            rows[status] = record["id"]
        seed.transition(rows["called"], "called")
        seed.transition(rows["absent"], "called")
        seed.transition(rows["absent"], "absent")
        stamp[0] = BASE + timedelta(minutes=2)
        for status in ("seated1", "seated2", "seated3"):
            seed.transition(rows[status], "seated")
        workspace.authorize_synthetic(database)
        migrate(workspace, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with workspace.connection(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required' "
                         "WHERE seat_corrections_enabled=0")
            if conn.execute("SELECT staff_write_mode,seat_corrections_enabled FROM store_metadata").fetchall() != [
                    ("revision_required", 0)]:
                raise LabConfigurationError("synthetic_database")
            conn.commit()
        frozen_time = BASE + timedelta(minutes=2)
        app = create_app(
            db_path=database, model_path=workspace.root / "no-model-in-this-synthetic-lab.joblib",
            staff_token=token, staff_browser_origin=f"https://staff.localhost:{port}",
            clock=lambda: frozen_time,
        )
        yield CallOnlyLab(app, port)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Call API only; disposable synthetic HTTPS lab. UI remains read-only.")
    parser.add_argument("--cert", required=True, help="Development TLS certificate outside this repository")
    parser.add_argument("--key", required=True, help="Owner-only development private key outside this repository")
    parser.add_argument("--port", type=int, default=8444)
    parser.add_argument("--check", action="store_true", help="Validate and prepare disposable DB without starting a server")
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
                print("[check_ok] TLS, credential and disposable call-only DB passed; no server started.", flush=True)
                return 0
            print(f"Synthetic call API only: https://staff.localhost:{args.port}/staff/login", flush=True)
            print("UI remains read-only. Run only one staff.localhost lab at a time (cookies share the host).", flush=True)
            stage = "server_startup"
            uvicorn.run(
                app, host="127.0.0.1", port=args.port, workers=1, reload=False,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                log_level="warning", ssl_certfile=cert, ssl_keyfile=key,
                ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER,
            )
            stage = "synthetic_database"
    except Exception as exc:
        code = exc.code if isinstance(exc, LabConfigurationError) else stage
        parser.exit(2, f"The call-only HTTPS lab could not start. [{code}] {DIAGNOSTICS[code]}\n")
    except SystemExit as exc:
        if stage != "server_startup" or exc.code in (None, 0):
            raise
        parser.exit(2, f"The call-only HTTPS lab could not start. [server_startup] {DIAGNOSTICS['server_startup']}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Call-only HTTPS lab cancelled.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
