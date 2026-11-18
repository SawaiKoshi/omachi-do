"""Customer page delivery and its existing same-origin reception contract."""

from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from prediction_api import create_app


STAFF_TOKEN = "customer-web-test-secret-never-served-1234567890"
ORIGIN = "http://127.0.0.1"
ASSETS = ("/assets/reception.css", "/assets/reception.mjs", "/assets/prediction-preview.mjs")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = None
        self.resources = []
        self.elements = {}
        self.sections = []
        self.forms = []
        self.inputs = []
        self.receipt_tags = []
        self.receipt_text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            assert values["id"] not in self.elements, "DOM IDs must be unique"
            self.elements[values["id"]] = {
                "tag": tag, "attrs": values,
                "sections": tuple(self.sections), "forms": tuple(self.forms),
            }
        if "receipt-view" in self.sections:
            self.receipt_tags.append(tag)
        if tag == "section":
            self.sections.append(values.get("id"))
        if tag == "form":
            self.forms.append(values.get("id"))
        if tag == "input":
            self.inputs.append(values)
        if tag == "html":
            self.lang = values.get("lang")
        if tag == "script" and values.get("src"):
            self.resources.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet":
            self.resources.append(values["href"])

    def handle_endtag(self, tag):
        if tag == "section":
            self.sections.pop()
        if tag == "form":
            self.forms.pop()

    def handle_data(self, data):
        if "receipt-view" in self.sections:
            self.receipt_text.append(data.strip())


@pytest.fixture
def customer_web(tmp_path, monkeypatch):
    def no_excel(*args, **kwargs):
        pytest.fail("Customer page and reception API must not read Excel")

    monkeypatch.setattr(pd, "read_excel", no_excel)
    database = tmp_path / "customer-web.sqlite3"
    app = create_app(
        db_path=database, model_path=tmp_path / "missing.joblib",
        staff_token=STAFF_TOKEN,
        clock=lambda: datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("Asia/Tokyo")),
    )
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        yield client, app, database


def test_japanese_page_loads_local_assets_without_changing_database(customer_web):
    client, _, database = customer_web
    before = database.read_bytes()
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "おまちどう" in page.text
    parser = PageResources()
    parser.feed(page.text)
    assert parser.lang == "ja"
    assert set(parser.resources) == set(ASSETS[:2])
    for path in ASSETS:
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset.content
        expected_type = "text/css" if path.endswith(".css") else "javascript"
        assert expected_type in asset.headers["content-type"]
    assert database.read_bytes() == before


def test_customer_page_keeps_browser_security_headers(customer_web):
    client, _, _ = customer_web
    page = client.get("/")
    assert page.headers["content-security-policy"] == CSP
    assert page.headers["referrer-policy"] == "no-referrer"
    for path in ("/", *ASSETS):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "access-control-allow-origin" not in response.headers


def test_dashboard_starts_at_home_with_an_inline_notice_and_only_party_inputs(customer_web):
    client, _, _ = customer_web
    parser = PageResources()
    parser.feed(client.get("/").text)
    elements = parser.elements
    assert "hidden" not in elements["home-view"]["attrs"]
    assert "hidden" in elements["entry-view"]["attrs"]
    assert "hidden" in elements["receipt-view"]["attrs"]
    assert "home-view" in elements["receipt-view"]["sections"]
    assert "receipt-view" not in elements["start-button"]["sections"]
    assert {item["name"] for item in parser.inputs} == {"adults", "children"}
    assert len(parser.inputs) == 2
    assert all(item["type"] == "number" for item in parser.inputs)
    assert elements["queue-count"]["sections"] == ("home-view",)


def test_compact_receipt_contains_only_number_party_size_and_close_control(customer_web):
    client, _, _ = customer_web
    parser = PageResources()
    parser.feed(client.get("/").text)
    elements = parser.elements
    receipt_ids = {
        element_id for element_id, element in elements.items()
        if "receipt-view" in element["sections"]
    }
    assert receipt_ids == {"receipt-title", "receipt-number", "receipt-people", "next-button"}
    assert elements["receipt-view"]["attrs"]["aria-live"] == "polite"
    assert elements["receipt-view"]["attrs"]["aria-labelledby"] == "receipt-title"
    assert elements["receipt-title"]["tag"] == "h2"
    assert elements["next-button"]["attrs"]["type"] == "button"
    assert "details" not in parser.receipt_tags
    text = " ".join(parser.receipt_text)
    assert "受付完了" in text
    for detail in ("前の待ち組数", "参考待ち時間について", "受付時点の情報"):
        assert detail not in text
    for removed in ("receipt-ahead", "receipt-wait", "receipt-warnings"):
        assert removed not in elements


def test_party_selection_keeps_counts_prediction_and_confirmation_in_one_form(customer_web):
    client, _, _ = customer_web
    parser = PageResources()
    parser.feed(client.get("/").text)
    elements = parser.elements
    assert elements["party-form"]["tag"] == "form"
    assert "entry-view" in elements["party-form"]["sections"]
    for element_id in (
        "adults", "children", "total-people", "entry-prediction", "entry-prediction-value",
        "form-message", "request-status", "submit-button",
    ):
        assert elements[element_id]["forms"] == ("party-form",)
        assert "entry-view" in elements[element_id]["sections"]
    assert elements["submit-button"]["attrs"]["type"] == "submit"
    assert {item["name"] for item in parser.inputs} == {"adults", "children"}
    assert len(parser.inputs) == 2
    for person in ("adults", "children"):
        attrs = elements[person]["attrs"]
        assert (attrs["min"], attrs["max"], attrs["step"]) == ("0", "100", "1")


@pytest.mark.parametrize("path", ("/", *ASSETS))
def test_page_and_assets_obey_existing_origin_host_and_peer_limits(customer_web, path):
    client, app, _ = customer_web
    assert client.get(path, headers={"Origin": ORIGIN}).status_code == 200
    assert client.get(path, headers={"Origin": "https://other.example"}).status_code == 403
    assert client.get(path, headers={"Host": "other.example"}).status_code == 400
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.1", 50000)) as remote:
        assert remote.get(path, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


@pytest.mark.parametrize("path", (
    "/assets/index.html",
    "/assets/prediction_api.py",
    "/assets/%2e%2e/index.html",
    "/assets/%2e%2e/%2e%2e/prediction_api.py",
    "/prediction_api.py",
    "/web/index.html",
))
def test_static_mount_does_not_expose_web_parent_or_repository(customer_web, path):
    client, _, _ = customer_web
    assert client.get(path).status_code == 404


def test_served_customer_code_contains_no_staff_secret_or_staff_api(customer_web):
    client, _, _ = customer_web
    served = "\n".join(client.get(path).text for path in ("/", *ASSETS))
    for private in (STAFF_TOKEN, "OMACHI_STAFF_TOKEN", "X-Staff-Token", '"/queue"', "'/queue'"):
        assert private not in served


def test_page_origin_can_register_and_replay_one_receipt_with_pre_intake_snapshot(customer_web):
    client, _, database = customer_web
    assert client.get("/").status_code == 200
    prior = client.post(
        "/receptions", json={"adults": 4, "children": 0},
        headers={"Origin": ORIGIN, "Idempotency-Key": str(uuid4())},
    )
    assert prior.status_code == 201
    key = str(uuid4())
    headers = {"Origin": ORIGIN, "Idempotency-Key": key}
    party = {"adults": 2, "children": 1}
    registered = client.post("/receptions", json=party, headers=headers)
    assert registered.status_code == 201
    receipt = registered.json()
    assert receipt["total_people"] == 3
    assert receipt["snapshot"]["waiting_groups"] == 1
    assert receipt["prediction"]["minutes"] is None
    assert receipt["prediction"]["warnings"]
    assert prior.json()["id"] not in registered.text
    replay = client.post("/receptions", json=party, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == receipt
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM receptions").fetchone()[0] == 2


def test_reference_and_selected_party_previews_do_not_register_or_allocate_numbers(customer_web, monkeypatch):
    client, app, database = customer_web
    registered = client.post("/receptions", json={"adults": 1, "children": 0},
                             headers={"Idempotency-Key": str(uuid4())})
    assert registered.status_code == 201
    before = database.read_bytes()
    parties = []

    def reference_prediction(**kwargs):
        parties.append((kwargs["adults"], kwargs["children"]))
        return {"status": "available", "minutes": 12.25, "model_version": "synthetic-preview",
                "predicted_at": kwargs["received_at"].isoformat(),
                "warnings": ["参考値です。ご案内順が前後する場合があります。"]}

    monkeypatch.setattr(app.state.predictor, "predict", reference_prediction)
    for adults, children in [(2, 0), (4, 0), (1, 2), (2, 0)]:
        # A preview needs neither an idempotency key nor staff authentication.
        response = client.post("/predict", json={"adults": adults, "children": children},
                               headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert response.json()["prediction"]["minutes"] == 12.25
        assert response.json()["snapshot"]["waiting_groups"] == 1
        assert "display_number" not in response.json()
        assert database.read_bytes() == before
    assert parties == [(2, 0), (4, 0), (1, 2), (2, 0)]
    assert len(app.state.store.get_queue()) == 1
