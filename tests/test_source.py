"""The source check: can the four CUM datasets be read, and do they look right?

The tests run against a small HTTP server on this machine that serves real
metadata and rows saved from datos.gov.co on 2026-11-22 (in tests/data/socrata).
Only the "Source check" workflow talks to datos.gov.co itself.
"""

import datetime
import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from colombia_medicines_catalog import source
from colombia_medicines_catalog.cli import main
from colombia_medicines_catalog.source import DATASETS, check_source

FIXTURES = Path(__file__).parent / "data" / "socrata"
VIEWS = FIXTURES / "views"
ROWS = FIXTURES / "resource" / "rows.json"


@dataclass
class FakeSocrata:
    """A local stand-in for datos.gov.co that records every request."""

    url: str = ""
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    # path -> (status, body, seconds to wait before answering)
    overrides: dict[str, tuple[int, bytes, float]] = field(default_factory=dict)

    def replace_metadata(self, dataset_id: str, change: dict[str, object]) -> None:
        metadata = json.loads(
            (VIEWS / f"{dataset_id}.json").read_text(encoding="utf-8")
        )
        metadata.update(change)
        body = json.dumps(metadata).encode()
        self.overrides[f"/api/views/{dataset_id}.json"] = (200, body, 0.0)


@pytest.fixture
def socrata() -> Iterator[FakeSocrata]:
    state = FakeSocrata()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append((self.path, dict(self.headers)))
            path = urlsplit(self.path).path
            if path in state.overrides:
                status, body, delay = state.overrides[path]
                time.sleep(delay)
                self.answer(status, body)
            elif match := re.fullmatch(r"/api/views/([a-z0-9-]+)\.json", path):
                metadata = VIEWS / f"{match[1]}.json"
                if metadata.is_file():
                    self.answer(200, metadata.read_bytes())
                else:
                    self.answer(404, b'{"error": true}')
            elif re.fullmatch(r"/resource/[a-z0-9-]+\.json", path):
                self.answer(200, ROWS.read_bytes())
            else:
                self.answer(404, b'{"error": true}')

        def answer(self, status: int, body: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up, as the timeout test intends

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def problems_by_dataset(base_url: str, **options: object) -> dict[str, list[str]]:
    return {
        check.dataset.id: list(check.problems)
        for check in check_source(base_url=base_url, **options)  # type: ignore[arg-type]
    }


def test_reachable_source_reports_every_dataset(socrata: FakeSocrata) -> None:
    checks = check_source(base_url=socrata.url)

    assert [check.dataset.id for check in checks] == [
        "i7cb-raxc",
        "vgr4-gemg",
        "vwwf-4ftk",
        "spzp-dfuc",
    ]
    assert all(check.ok for check in checks)
    vigentes = checks[0]
    assert vigentes.name == "CÓDIGO ÚNICO DE MEDICAMENTOS VIGENTES"
    assert vigentes.updated_on == datetime.date(2026, 11, 16)
    assert vigentes.license == "CC_40_BY_SA"
    assert vigentes.columns == 29
    assert vigentes.sample_rows == 2


def test_datasets_cover_the_four_registration_states() -> None:
    assert {dataset.status for dataset in DATASETS} == {
        "vigentes",
        "en_tramite_renovacion",
        "vencidos",
        "otros_estados",
    }


def test_each_dataset_is_read_with_one_small_ordered_page(socrata: FakeSocrata) -> None:
    check_source(base_url=socrata.url)

    pages = [urlsplit(path) for path, _ in socrata.requests if "/resource/" in path]
    assert len(pages) == 4
    for page in pages:
        assert parse_qs(page.query) == {"$limit": ["5"], "$order": [":id"]}


def test_token_is_sent_as_a_header_and_never_in_the_url(socrata: FakeSocrata) -> None:
    check_source(base_url=socrata.url, token="secret-token")

    assert socrata.requests
    for path, headers in socrata.requests:
        assert headers.get("X-App-Token") == "secret-token"
        assert "secret-token" not in path


def test_no_token_sends_no_token_header(socrata: FakeSocrata) -> None:
    check_source(base_url=socrata.url)

    assert all("X-App-Token" not in headers for _, headers in socrata.requests)


def test_metadata_for_another_dataset_is_a_problem(socrata: FakeSocrata) -> None:
    socrata.replace_metadata("i7cb-raxc", {"id": "abcd-1234"})

    problems = problems_by_dataset(socrata.url)

    assert problems["i7cb-raxc"] == ["the metadata describes 'abcd-1234'"]
    assert problems["vgr4-gemg"] == []


def test_missing_key_column_is_a_problem(socrata: FakeSocrata) -> None:
    metadata = json.loads((VIEWS / "vwwf-4ftk.json").read_text(encoding="utf-8"))
    columns = [c for c in metadata["columns"] if c["fieldName"] != "consecutivocum"]
    socrata.replace_metadata("vwwf-4ftk", {"columns": columns})

    problems = problems_by_dataset(socrata.url)

    assert problems["vwwf-4ftk"] == [
        "column 'consecutivocum' is missing from the metadata"
    ]


def test_empty_first_page_is_a_problem(socrata: FakeSocrata) -> None:
    socrata.overrides["/resource/spzp-dfuc.json"] = (200, b"[]", 0.0)

    problems = problems_by_dataset(socrata.url)

    assert problems["spzp-dfuc"] == ["the first page has no rows"]


def test_rows_without_key_fields_are_a_problem(socrata: FakeSocrata) -> None:
    socrata.overrides["/resource/vgr4-gemg.json"] = (200, b'[{"producto": "X"}]', 0.0)

    problems = problems_by_dataset(socrata.url)

    assert problems["vgr4-gemg"] == [
        "rows have no 'expedientecum' field",
        "rows have no 'consecutivocum' field",
    ]


def test_http_error_is_reported_with_the_url(socrata: FakeSocrata) -> None:
    socrata.overrides["/api/views/i7cb-raxc.json"] = (503, b"busy", 0.0)

    checks = {check.dataset.id: check for check in check_source(base_url=socrata.url)}

    assert checks["i7cb-raxc"].problems == (
        f"{socrata.url}/api/views/i7cb-raxc.json answered HTTP 503",
    )
    assert checks["i7cb-raxc"].updated_on is None
    assert checks["vgr4-gemg"].ok


def test_invalid_json_is_reported(socrata: FakeSocrata) -> None:
    socrata.overrides["/resource/vgr4-gemg.json"] = (
        200,
        b"<html>maintenance</html>",
        0.0,
    )

    problems = problems_by_dataset(socrata.url)

    assert problems["vgr4-gemg"] == [
        f"{socrata.url}/resource/vgr4-gemg.json?%24limit=5&%24order=%3Aid "
        "did not return valid JSON"
    ]


def test_response_with_an_unexpected_shape_is_reported(socrata: FakeSocrata) -> None:
    socrata.overrides["/api/views/vwwf-4ftk.json"] = (200, b"[1, 2, 3]", 0.0)
    socrata.overrides["/resource/spzp-dfuc.json"] = (200, b'{"rows": []}', 0.0)

    problems = problems_by_dataset(socrata.url)

    assert problems["vwwf-4ftk"] == [
        f"{socrata.url}/api/views/vwwf-4ftk.json returned something other than "
        "dataset metadata"
    ]
    assert problems["spzp-dfuc"] == [
        f"{socrata.url}/resource/spzp-dfuc.json?%24limit=5&%24order=%3Aid returned "
        "something other than a list of rows"
    ]


def test_slow_answer_is_reported_as_a_timeout(socrata: FakeSocrata) -> None:
    socrata.overrides["/api/views/i7cb-raxc.json"] = (200, b"{}", 2.0)

    problems = problems_by_dataset(socrata.url, timeout=0.2)

    (problem,) = problems["i7cb-raxc"]
    assert problem.startswith(f"could not reach {socrata.url}/api/views/i7cb-raxc.json")
    assert "timed out" in problem


def test_unreachable_host_is_reported() -> None:
    # Port 9 (discard) is closed on test machines, so the connection is refused.
    problems = problems_by_dataset("http://127.0.0.1:9", timeout=2)

    assert all(
        len(found) == 1 and found[0].startswith("could not reach http://127.0.0.1:9/")
        for found in problems.values()
    )


def test_cli_check_source_prints_each_dataset_and_exits_0(
    socrata: FakeSocrata,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(source, "BASE_URL", socrata.url)
    monkeypatch.delenv("SOCRATA_APP_TOKEN", raising=False)

    exit_code = main(["check-source"])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert lines[0] == "No app token: requests share the anonymous limit."
    assert lines[1] == (
        "i7cb-raxc  vigentes  updated 2026-11-16  29 columns  CC_40_BY_SA  "
        "sample of 2 rows  ok"
    )
    assert lines[-1] == "All 4 datasets can be read."


def test_cli_check_source_lists_problems_and_exits_1(
    socrata: FakeSocrata,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(source, "BASE_URL", socrata.url)
    socrata.overrides["/resource/spzp-dfuc.json"] = (200, b"[]", 0.0)

    exit_code = main(["check-source"])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 1
    assert "spzp-dfuc  otros_estados  PROBLEM" in lines
    assert "  - the first page has no rows" in lines
    assert lines[-1] == "1 of 4 datasets have problems."


def test_cli_check_source_uses_the_token_without_printing_it(
    socrata: FakeSocrata,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(source, "BASE_URL", socrata.url)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "secret-token")

    main(["check-source"])

    output = capsys.readouterr().out
    assert output.splitlines()[0] == "Using the app token in SOCRATA_APP_TOKEN."
    assert "secret-token" not in output
    assert all(
        headers.get("X-App-Token") == "secret-token" for _, headers in socrata.requests
    )


def test_cli_check_source_ignores_an_empty_token(
    socrata: FakeSocrata,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A GitHub secret that is not set reaches the workflow as an empty string.
    monkeypatch.setattr(source, "BASE_URL", socrata.url)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "")

    main(["check-source"])

    assert capsys.readouterr().out.startswith("No app token")
    assert all("X-App-Token" not in headers for _, headers in socrata.requests)
