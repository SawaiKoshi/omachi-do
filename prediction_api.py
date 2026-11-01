"""ローカル開発用の受付管理・参考待ち時間予測API。import時のI/O・学習なし。"""

from contextlib import asynccontextmanager
from datetime import datetime
import hmac
from ipaddress import ip_address
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from prediction_service import PredictionService, QUEUE_ORDER_NOTICE
from reception_store import (
    IdempotencyConflict, ReceptionNotFound, ReceptionStore, StateConflict,
    StoreBusy, StoreValidationError,
)
from staff_operation import MAX_REVISION, uuid_text
from quality_review_store import QualityReviewStore
from staff_read_store import StaffReadStore
from staff_sessions import (
    ABSOLUTE_SECONDS, COOKIE_NAME, SHARED_PRINCIPAL, SessionCapacity, SessionRejected,
    StaffSessions, browser_origin,
)


class PartyInput(BaseModel):
    """お客様が入力する人数だけ。時刻・番号・混雑情報は受け取らない。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    adults: int = Field(ge=0, le=100)
    children: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def total_in_range(self):
        if not 1 <= self.adults + self.children <= 100:
            raise ValueError("合計人数は1〜100人で指定してください。")
        return self


class PredictionOutput(BaseModel):
    status: Literal[
        "available", "model_unavailable", "no_waiting_groups",
        "unsupported_weekday", "unsupported_time", "prediction_failed",
    ]
    minutes: float | None
    model_version: str | None
    predicted_at: str
    warnings: list[str]


class QueueSnapshot(BaseModel):
    captured_at: str
    source: str
    waiting_groups: int
    ahead_groups: int
    ahead_same_size_groups: int
    party_size_groups: dict[str, int]


class PredictionResponse(BaseModel):
    prediction: PredictionOutput
    snapshot: QueueSnapshot


class ReceptionResponse(BaseModel):
    id: str
    display_number: str
    reception_order: int
    received_at: str
    adults: int
    children: int
    total_people: int
    status: Literal["waiting", "called", "absent", "seated", "cancelled"]
    called_at: str | None
    seated_at: str | None
    cancelled_at: str | None
    actual_wait_minutes: float | None
    snapshot: QueueSnapshot
    prediction: PredictionOutput


class StaffOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    action: Literal["call", "absent", "return", "seat", "cancel"]
    expected_revision: int = Field(ge=0, le=MAX_REVISION)
    write_epoch: str

    @model_validator(mode="after")
    def normalize_identifiers(self):
        self.operation_id = uuid_text(self.operation_id)
        self.write_epoch = uuid_text(self.write_epoch)
        return self


class StaffReceptionResponse(BaseModel):
    reception: ReceptionResponse
    revision: int
    write_epoch: str
    staff_write_mode: Literal["legacy", "revision_required"]


class SeatingCorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    expected_revision: int = Field(ge=0, le=MAX_REVISION)
    write_epoch: str
    target_event_id: int = Field(ge=1, le=MAX_REVISION)
    reason_code: Literal["wrong_reception", "accidental_seating", "other"]
    reason_detail: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_correction(self):
        self.operation_id = uuid_text(self.operation_id)
        self.write_epoch = uuid_text(self.write_epoch)
        if not self.reason_detail.strip():
            raise ValueError("訂正理由を記録してください。")
        return self


class StaffEventsResponse(BaseModel):
    events: list[dict]


class StaffDataQualityResponse(BaseModel):
    reception_id: str
    training_use: Literal["blocked"]
    issues: list[dict]
    unresolved_issue_ids: list[int]
    reasons: list[str]


class StaffOperationError(BaseModel):
    code: str
    detail: str


class StaffOperationResponse(BaseModel):
    operation_id: str
    reception_id: str
    action: Literal["call", "absent", "return", "seat", "cancel", "seating_correction"]
    outcome: Literal["state_changed", "no_change", "rejected"]
    response_status: int
    recorded_at: str
    resulting_revision: int | None
    reception: ReceptionResponse | None
    error: StaffOperationError | None


class QualityWatermarks(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: int = Field(ge=0, le=MAX_REVISION)
    issue_id: int = Field(ge=0, le=MAX_REVISION)
    review_id: int = Field(ge=0, le=MAX_REVISION)


class QualityCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    expected_revision: int = Field(ge=0, le=MAX_REVISION)
    write_epoch: str
    expected_watermarks: QualityWatermarks
    reason_code: str = Field(min_length=1, max_length=64)
    reason_detail: str = Field(min_length=1, max_length=500)
    evidence: dict

    @model_validator(mode="after")
    def validate_identifiers(self):
        self.operation_id = uuid_text(self.operation_id)
        self.write_epoch = uuid_text(self.write_epoch)
        if not self.reason_code.strip() or not self.reason_detail.strip():
            raise ValueError("品質判断の理由を記録してください。")
        return self


class QualityReviewInput(QualityCommandInput):
    issue_id: int = Field(ge=1, le=MAX_REVISION)
    expected_review_number: int = Field(ge=0, le=MAX_REVISION)
    decision: Literal["hold", "exclude", "clear"]


class QualityAssessmentInput(QualityCommandInput):
    expected_assessment_id: int = Field(ge=0, le=MAX_REVISION)
    decision: Literal["hold", "exclude", "eligible"]


class QualityOperationResponse(BaseModel):
    operation_id: str
    reception_id: str
    action: Literal["quality_review", "quality_assessment"]
    outcome: Literal["review_added", "assessment_added", "rejected"]
    response_status: int
    recorded_at: str
    resulting_revision: int | None
    quality_result: dict | None
    error: StaffOperationError | None


class QueueGroup(BaseModel):
    id: str
    display_number: str
    received_at: str
    adults: int
    children: int
    total_people: int
    status: Literal["waiting", "called", "absent"]
    called_at: str | None


class QueueResponse(BaseModel):
    waiting_groups: int
    groups: list[QueueGroup]
    notice: str


class PublicQueueGroup(BaseModel):
    """お客様の待ち状況表示に必要な情報だけ。内部ID・履歴は公開しない。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    display_number: str
    total_people: int = Field(ge=1, le=100)
    status: Literal["waiting", "called", "absent"]


class PublicQueueResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    waiting_groups: int = Field(ge=0)
    groups: list[PublicQueueGroup]
    notice: str


def create_app(
    *, db_path: str | Path | None = None, model_path: str | Path | None = None,
    staff_token: str | None = None, clock=None, staff_browser_origin: str | None = None,
) -> FastAPI:
    """Uvicornのfactory。DBと信頼済みモデルはlifespan開始時だけ開く。"""
    database = Path(db_path or os.environ.get("OMACHI_DB_PATH", "var/receptions.sqlite3"))
    trusted_model = model_path if model_path is not None else os.environ.get("OMACHI_MODEL_PATH")
    token = staff_token if staff_token is not None else os.environ.get("OMACHI_STAFF_TOKEN")
    if token is not None and (len(token) < 32 or not token.strip()):
        raise ValueError("OMACHI_STAFF_TOKENは32文字以上のランダムな値にしてください。")
    session_origin = browser_origin(staff_browser_origin)

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = ReceptionStore(database, clock=clock)
        app.state.predictor = PredictionService(trusted_model)
        app.state.staff_reads = StaffReadStore(app.state.store)
        app.state.staff_sessions = StaffSessions()
        try:
            yield
        finally:
            app.state.staff_sessions.clear()

    app = FastAPI(title="おまちどう（ローカル開発用）", lifespan=lifespan, openapi_url=None)
    web_directory = Path(__file__).resolve().parent / "web"
    app.mount("/assets", StaticFiles(directory=web_directory / "static", check_dir=False),
              name="customer-assets")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
        + (["staff.localhost"] if session_origin is not None else []),
    )

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        try:
            local = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            return JSONResponse(status_code=403, content={"detail": "ローカル接続専用です。"},
                                headers={"Cache-Control": "no-store"})
        # A cookie is scoped by hostname, not port. Keep the staff origin on a
        # separate host and never serve customer HTML/JS or public APIs there.
        if request.url.hostname == "staff.localhost":
            if (session_origin is None or str(request.base_url).rstrip("/") != session_origin
                    or not request.url.path.startswith("/staff/")
                    or any(name in request.headers for name in (
                        "forwarded", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-for"))):
                return JSONResponse(status_code=403, content={"detail": "スタッフ専用HTTPS接続が必要です。"},
                                    headers={"Cache-Control": "no-store"})
        # 別サイトからのブラウザ操作を拒否。CORSやproxy転送は有効にしない。
        origin = request.headers.get("origin")
        if (len(request.headers.getlist("origin")) > 1
                or origin is not None and origin != str(request.base_url).rstrip("/")):
            return JSONResponse(status_code=403, content={"detail": "異なるオリジンからの操作はできません。"},
                                headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/staff/"):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    def require_credential(request, x_staff_token):
        if token is None:
            raise HTTPException(503, "スタッフ操作は未設定です。OMACHI_STAFF_TOKENを設定してください。")
        if (len(request.headers.getlist("x-staff-token")) != 1 or x_staff_token is None
                or not hmac.compare_digest(x_staff_token.encode(), token.encode())):
            raise HTTPException(401, "スタッフ認証が必要です。")

    def session_cookie(request):
        parts = [part.split("=", 1) for header in request.headers.getlist("cookie")
                 for part in header.split(";") if "=" in part]
        values = [value.strip() for name, value in parts if name.strip() == COOKIE_NAME]
        if len(values) > 1:
            raise HTTPException(401, "スタッフセッションを確認できません。")
        return values[0] if values else None

    def require_session_origin(request, *, writing=False):
        if session_origin is None or token is None:
            raise HTTPException(503, "ブラウザ用スタッフ認証は無効です。")
        if str(request.base_url).rstrip("/") != session_origin:
            raise HTTPException(403, "スタッフ専用HTTPS接続が必要です。")
        if writing and (request.headers.getlist("origin") != [session_origin]):
            raise HTTPException(403, "同一オリジンの確認が必要です。")

    def authenticated_session(request, *, writing=False):
        require_session_origin(request, writing=writing)
        if writing and len(request.headers.getlist("x-csrf-token")) != 1:
            raise HTTPException(403, "CSRF確認が必要です。")
        try:
            return request.app.state.staff_sessions.authenticate(
                session_cookie(request), csrf=request.headers.get("x-csrf-token"), require_csrf=writing)
        except SessionRejected:
            raise HTTPException(401, "スタッフセッションまたはCSRFを確認できません。") from None

    def require_staff(request: Request, x_staff_token: Annotated[str | None, Header()] = None):
        # Cookie failures never fall back to a supplied header credential.
        cookie = session_cookie(request)
        if cookie is not None or str(request.base_url).rstrip("/") == session_origin:
            writing = request.method not in ("GET", "HEAD", "OPTIONS")
            authenticated_session(request, writing=writing)
            if not request.url.path.startswith("/staff/"):
                raise HTTPException(403, "ブラウザ認証では旧更新APIを利用できません。")
            if writing and request.scope["route"].path not in (
                "/staff/receptions/{reception_id}/operations",
                "/staff/receptions/{reception_id}/seating-corrections",
            ):
                # Browser sessions do not receive quality review/release powers.
                raise HTTPException(403, "ブラウザからこの管理操作は利用できません。")
            if writing and not request.app.state.staff_reads.configuration()["write_enabled"]:
                raise HTTPException(409, "保護付き更新モードが必要です。")
            request.state.staff_browser_authenticated = True
        else:
            require_credential(request, x_staff_token)
        # Shared role only; no claimed individual identity or token fingerprint.
        return dict(SHARED_PRINCIPAL)

    @app.post("/staff/session")
    def login(request: Request, response: Response,
              x_staff_token: Annotated[str | None, Header()] = None):
        require_session_origin(request, writing=True)
        if not request.app.state.staff_sessions.allow_login():
            raise HTTPException(429, "時間をおいて再試行してください。", headers={"Retry-After": "60"})
        require_credential(request, x_staff_token)
        try:
            cookie, _ = request.app.state.staff_sessions.create(session_cookie(request))
        except SessionCapacity:
            raise HTTPException(503, "スタッフセッションの上限に達しました。") from None
        response.set_cookie(COOKIE_NAME, cookie, max_age=ABSOLUTE_SECONDS,
                            secure=True, httponly=True, samesite="strict", path="/")
        return request.app.state.staff_sessions.authenticate(cookie)

    @app.get("/staff/session")
    def current_session(request: Request):
        return authenticated_session(request)

    @app.delete("/staff/session")
    def logout(request: Request, response: Response):
        authenticated_session(request, writing=True)
        request.app.state.staff_sessions.revoke(session_cookie(request))
        response.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="strict", path="/")
        return {"authenticated": False}

    async def require_no_body(request: Request):
        if await request.body():
            raise HTTPException(422, "操作の本文は不要です。日時はシステムが記録します。")

    staff_operations = [Depends(require_staff), Depends(require_no_body)]

    @app.get("/", include_in_schema=False)
    def customer_reception():
        return FileResponse(web_directory / "index.html", headers={
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
        })

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # 余剰入力などの値自体（氏名等が送られた場合も）を応答へ反映しない。
        errors = [{"loc": item["loc"], "type": item["type"], "msg": item["msg"]}
                  for item in error.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(StoreValidationError)
    async def invalid_store_input(request, error):
        return JSONResponse(status_code=422, content={"detail": "人数またはシステム日時が不正です。"})

    @app.exception_handler(ReceptionNotFound)
    async def not_found(request, error):
        return JSONResponse(status_code=404, content={"detail": "受付が見つかりません。"})

    @app.exception_handler(StateConflict)
    async def invalid_state(request, error):
        return JSONResponse(status_code=409, content={"detail": "この状態から操作できません。"})

    @app.exception_handler(IdempotencyConflict)
    async def repeated_different_input(request, error):
        return JSONResponse(status_code=409, content={"detail": "同じ送信キーに異なる人数が指定されました。"})

    @app.exception_handler(StoreBusy)
    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        # DB失敗を受付成功と扱わず、再送時も同じキーを使えるようにする。
        return JSONResponse(status_code=503, content={"detail": "保存処理を完了できません。同じ送信キーで再試行してください。"},
                            headers={"Retry-After": "1"})

    @app.get("/health")
    def health(request: Request):
        request.app.state.store.get_queue()
        return {"status": "ok", "model_status": "available" if request.app.state.predictor.available else "unavailable",
                "staff_operations_enabled": token is not None}

    @app.post("/predict", response_model=PredictionResponse)
    def predict(party: PartyInput, request: Request):
        snapshot = request.app.state.store.snapshot(party.adults + party.children)
        result = request.app.state.predictor.predict(
            adults=party.adults, children=party.children,
            received_at=datetime.fromisoformat(snapshot["captured_at"]),
            waiting_groups=snapshot["waiting_groups"],
        )
        return {"prediction": result, "snapshot": snapshot}

    @app.post("/receptions", response_model=ReceptionResponse, status_code=201)
    def register(
        party: PartyInput, request: Request, response: Response,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ):
        record, created = request.app.state.store.register(
            party.adults, party.children, str(idempotency_key),
            predictor=request.app.state.predictor.predict,
        )
        response.status_code = 201 if created else 200
        response.headers["Idempotent-Replayed"] = "false" if created else "true"
        return record

    @app.get("/queue", response_model=QueueResponse, dependencies=[Depends(require_staff)])
    def queue(request: Request):
        groups = request.app.state.store.get_queue()
        return {"waiting_groups": len(groups), "groups": groups, "notice": QUEUE_ORDER_NOTICE}

    @app.get("/public/queue", response_model=PublicQueueResponse)
    def public_queue(request: Request):
        # 同じ読取結果から一覧と総数を作る。日付またぎの未案内受付も受付順で残す。
        groups = request.app.state.store.get_queue()
        public_groups = [
            {"display_number": group["display_number"],
             "total_people": group["total_people"], "status": group["status"]}
            for group in groups
        ]
        return {"waiting_groups": len(public_groups), "groups": public_groups,
                "notice": QUEUE_ORDER_NOTICE}

    @app.get("/staff/receptions/{reception_id}", response_model=StaffReceptionResponse,
             dependencies=[Depends(require_staff)])
    def staff_reception(reception_id: UUID, request: Request):
        return request.app.state.store.get_staff_reception(str(reception_id))

    @app.get("/staff/receptions", dependencies=[Depends(require_staff)])
    def staff_receptions(request: Request, view: Literal["active", "recently_seated"] = "active",
                         limit: Annotated[int, Query(ge=1, le=100)] = 50,
                         hours: Annotated[int, Query(ge=1, le=168)] = 24,
                         cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None):
        return request.app.state.staff_reads.listing(view=view, limit=limit, hours=hours, cursor=cursor)

    @app.get("/staff/receptions/{reception_id}/operation-context", dependencies=[Depends(require_staff)])
    def staff_operation_context(reception_id: UUID, request: Request):
        return request.app.state.staff_reads.detail(str(reception_id))

    @app.get("/staff/receptions/{reception_id}/events", response_model=StaffEventsResponse,
             dependencies=[Depends(require_staff)])
    def staff_events(reception_id: UUID, request: Request):
        return {"events": request.app.state.store.get_staff_events(str(reception_id))}

    @app.get("/staff/receptions/{reception_id}/data-quality", response_model=StaffDataQualityResponse,
             dependencies=[Depends(require_staff)])
    def staff_data_quality(reception_id: UUID, request: Request):
        return request.app.state.store.get_data_quality(str(reception_id))

    @app.get("/staff/receptions/{reception_id}/quality-context", dependencies=[Depends(require_staff)])
    def quality_context(reception_id: UUID, request: Request):
        return QualityReviewStore(request.app.state.store).context(str(reception_id))

    @app.post("/staff/receptions/{reception_id}/quality-reviews", response_model=QualityOperationResponse)
    def quality_review(reception_id: UUID, command: QualityReviewInput, request: Request,
                       principal: Annotated[dict, Depends(require_staff)]):
        # The HTTP app has no release verifier. Shared authentication can only
        # append hold/exclude decisions, even if callers request clear/eligible.
        result = QualityReviewStore(request.app.state.store).review_issue(
            str(reception_id), **command.model_dump(), **principal)
        return JSONResponse(status_code=result["response_status"],
                            content=QualityOperationResponse.model_validate(result).model_dump(mode="json"))

    @app.post("/staff/receptions/{reception_id}/quality-assessments", response_model=QualityOperationResponse)
    def quality_assessment(reception_id: UUID, command: QualityAssessmentInput, request: Request,
                           principal: Annotated[dict, Depends(require_staff)]):
        result = QualityReviewStore(request.app.state.store).assess(
            str(reception_id), **command.model_dump(), **principal)
        return JSONResponse(status_code=result["response_status"],
                            content=QualityOperationResponse.model_validate(result).model_dump(mode="json"))

    @app.post("/staff/receptions/{reception_id}/seating-corrections", response_model=StaffOperationResponse)
    def seating_correction(reception_id: UUID, command: SeatingCorrectionInput, request: Request,
                           principal: Annotated[dict, Depends(require_staff)]):
        result = request.app.state.store.correct_seating(str(reception_id), **command.model_dump(), **principal)
        body = StaffOperationResponse.model_validate(result).model_dump(mode="json")
        return JSONResponse(status_code=result["response_status"], content=body)

    @app.post("/staff/receptions/{reception_id}/operations", response_model=StaffOperationResponse)
    def protected_operation(reception_id: UUID, command: StaffOperationInput, request: Request,
                            principal: Annotated[dict, Depends(require_staff)]):
        result = request.app.state.store.staff_transition(
            str(reception_id), command.action, operation_id=command.operation_id,
            expected_revision=command.expected_revision, write_epoch=command.write_epoch, **principal,
            require_revision_mode=getattr(request.state, "staff_browser_authenticated", False),
        )
        # Validate and filter even persisted responses before returning them.
        body = StaffOperationResponse.model_validate(result).model_dump(mode="json")
        return JSONResponse(status_code=result["response_status"], content=body)

    @app.get("/staff/operations/{operation_id}", response_model=StaffOperationResponse | QualityOperationResponse)
    def staff_operation_result(operation_id: UUID, request: Request,
                               principal: Annotated[dict, Depends(require_staff)]):
        # Lookup itself is 200; response_status describes the original command.
        return request.app.state.store.get_staff_operation(str(operation_id), **principal)

    def operation(reception_id: UUID, request: Request, target: str):
        return request.app.state.store.transition(str(reception_id), target)

    @app.post("/receptions/{reception_id}/seat", response_model=ReceptionResponse, dependencies=staff_operations)
    def seat(reception_id: UUID, request: Request):
        return operation(reception_id, request, "seated")

    @app.post("/receptions/{reception_id}/cancel", response_model=ReceptionResponse, dependencies=staff_operations)
    def cancel(reception_id: UUID, request: Request):
        return operation(reception_id, request, "cancelled")

    @app.post("/receptions/{reception_id}/call", response_model=ReceptionResponse, dependencies=staff_operations)
    def call(reception_id: UUID, request: Request):
        return operation(reception_id, request, "called")

    @app.post("/receptions/{reception_id}/absent", response_model=ReceptionResponse, dependencies=staff_operations)
    def absent(reception_id: UUID, request: Request):
        return operation(reception_id, request, "absent")

    @app.post("/receptions/{reception_id}/return", response_model=ReceptionResponse, dependencies=staff_operations)
    def return_to_queue(reception_id: UUID, request: Request):
        return operation(reception_id, request, "waiting")

    return app
