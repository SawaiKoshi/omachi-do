"""Disposable seat-only HTTPS lab. Start with python -m staff_seat_https_lab --cert ... --key ..."""

from contextlib import contextmanager

from starlette.responses import JSONResponse

from staff_call_absent_https_lab import (
    InvalidOperationBody, OPERATIONS_PATH, read_operation_request, run_https_lab, synthetic_protected_app,
)
from staff_https_lab import ReadOnlyLab

PORT = 8446
AUTHORITY = b"staff.localhost:8446"


class SeatOnlyLab:
    def __init__(self, app):
        self.app = app
        self.readonly = ReadOnlyLab(app)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.readonly(scope, receive, send)
            return

        async def preview_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", ())
                           if k.lower() not in (b"x-omachi-preview", b"x-omachi-preview-actions")]
                message = {**message, "headers": [*headers, (b"x-omachi-preview", b"synthetic"),
                                                  (b"x-omachi-preview-actions", b"seat")]}
            await send(message)

        async def reject(status):
            response = JSONResponse({"detail": "Seat-only synthetic HTTPS request rejected."},
                                    status_code=status, headers={"Cache-Control": "no-store"})
            await response(scope, receive, preview_send)

        hosts = [v for k, v in scope["headers"] if k.lower() == b"host"]
        if scope["scheme"] != "https" or hosts != [AUTHORITY]:
            await reject(403)
            return
        if scope["method"] != "POST" or not OPERATIONS_PATH.fullmatch(scope["path"]):
            await self.readonly(scope, receive, preview_send)
            return
        try:
            parsed = await read_operation_request(scope, receive)
        except InvalidOperationBody as exc:
            await reject(exc.status)
            return
        if parsed is None:
            return
        command, replay_receive = parsed
        if command.action != "seat":
            await reject(403)
            return

        # The original bytes go through the app's session, Origin, CSRF and revision checks.
        await self.app(scope, replay_receive, preview_send)


@contextmanager
def synthetic_app(token, port=PORT):
    if port != PORT:
        raise ValueError("Seat lab requires port 8446.")
    with synthetic_protected_app(token, PORT, microseconds=123456) as app:
        yield SeatOnlyLab(app)


def main(argv=None):
    return run_https_lab(argv, port=PORT, factory=synthetic_app, label="seat",
                        usage="Seat API only; browser seat controls are not connected.")


if __name__ == "__main__":
    raise SystemExit(main())
