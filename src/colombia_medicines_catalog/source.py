"""The four CUM datasets that INVIMA publishes on datos.gov.co.

`check_source` makes a quick check that every dataset can be read: it asks
for its metadata and for a first page of five rows, and reports what looks
wrong. It downloads nothing else. The full download comes later.

The data is published under Creative Commons Attribution-ShareAlike 4.0 by
INVIMA (Instituto Nacional de Vigilancia de Medicamentos y Alimentos).
"""

import datetime
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from colombia_medicines_catalog import __version__

BASE_URL = "https://www.datos.gov.co"
SAMPLE_ROWS = 5
TIMEOUT_SECONDS = 30.0
# Together they identify one commercial presentation of a medicine.
KEY_COLUMNS = ("expedientecum", "consecutivocum")


@dataclass(frozen=True)
class Dataset:
    """One CUM dataset: its id on datos.gov.co and the registrations it holds."""

    id: str
    status: str


DATASETS = (
    Dataset("i7cb-raxc", "vigentes"),
    Dataset("vgr4-gemg", "en_tramite_renovacion"),
    Dataset("vwwf-4ftk", "vencidos"),
    Dataset("spzp-dfuc", "otros_estados"),
)


class SourceError(Exception):
    """A request to datos.gov.co failed. The message names the URL."""


@dataclass(frozen=True)
class DatasetCheck:
    """What the check found for one dataset."""

    dataset: Dataset
    name: str = ""
    updated_on: datetime.date | None = None
    license: str = ""
    columns: int = 0
    sample_rows: int = 0
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def check_source(
    base_url: str | None = None,
    token: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
    datasets: Sequence[Dataset] = DATASETS,
) -> list[DatasetCheck]:
    """Check every dataset and return one result per dataset, in order.

    A failed request is reported as a problem of its dataset, so one dataset
    that cannot be read does not hide the state of the others.
    """
    base = base_url or BASE_URL
    checks = []
    for dataset in datasets:
        try:
            checks.append(_check_dataset(dataset, base, token, timeout))
        except SourceError as error:
            checks.append(DatasetCheck(dataset=dataset, problems=(str(error),)))
    return checks


def fetch_json(
    url: str, token: str | None = None, timeout: float = TIMEOUT_SECONDS
) -> object:
    """Return the JSON at `url`. The app token, if any, goes in a header."""
    headers = {"Accept": "application/json", "User-Agent": f"colmed/{__version__}"}
    if token:
        headers["X-App-Token"] = token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        error.close()  # the error carries the open response
        raise SourceError(f"{url} answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise SourceError(f"could not reach {url}: {reason}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError(f"{url} did not return valid JSON") from None


def _check_dataset(
    dataset: Dataset, base: str, token: str | None, timeout: float
) -> DatasetCheck:
    metadata_url = f"{base}/api/views/{dataset.id}.json"
    metadata = fetch_json(metadata_url, token, timeout)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("columns"), list):
        raise SourceError(
            f"{metadata_url} returned something other than dataset metadata"
        )

    query = urlencode({"$limit": SAMPLE_ROWS, "$order": ":id"})
    page_url = f"{base}/resource/{dataset.id}.json?{query}"
    rows = fetch_json(page_url, token, timeout)
    if not isinstance(rows, list):
        raise SourceError(f"{page_url} returned something other than a list of rows")

    columns = {column.get("fieldName") for column in metadata["columns"]}
    problems = []
    if metadata.get("id") != dataset.id:
        problems.append(f"the metadata describes {metadata.get('id')!r}")
    problems += [
        f"column {name!r} is missing from the metadata"
        for name in KEY_COLUMNS
        if name not in columns
    ]
    if not rows:
        problems.append("the first page has no rows")
    else:
        problems += [
            f"rows have no {name!r} field"
            for name in KEY_COLUMNS
            if not all(isinstance(row, dict) and name in row for row in rows)
        ]

    updated = metadata.get("rowsUpdatedAt")
    return DatasetCheck(
        dataset=dataset,
        name=str(metadata.get("name", "")),
        updated_on=(
            datetime.datetime.fromtimestamp(updated, datetime.UTC).date()
            if isinstance(updated, int)
            else None
        ),
        license=str(metadata.get("licenseId", "")),
        columns=len(metadata["columns"]),
        sample_rows=len(rows),
        problems=tuple(problems),
    )
