"""Bounded dataset imports and single-example runs shared by UI and CLI."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from pydantic import ValidationError

from giraffe.caps import MAX_DATASET_PREVIEW_ROWS

from giraffe.models import (MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES,
                            ConfigDraft, RequestPack, RunConfig)
from giraffe.request_pack import pack_warnings


def read_dataset(path: Path) -> str:
    with path.open("rb") as source:
        raw = source.read(MAX_REQUEST_PACK_BYTES + 1)
    if len(raw) > MAX_REQUEST_PACK_BYTES:
        raise ValueError("Dataset exceeds 1 MiB")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise ValueError("Dataset must be UTF-8 text") from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate field names")
        result[key] = value
    return result


def preview_dataset(text: str, format: str, *, input_field: str | None = None,
                    expected_field: str | None = None, scorer: str = "judge") -> dict:
    result = {"columns": [], "preview_rows": [], "suggested_mapping": {
        "input_field": None, "expected_field": None}, "request_pack": None,
        "errors": [], "warnings": [], "needs_mapping": False}

    def error(field, message, row=None):
        result["errors"].append({"row": row, "field": field, "message": message})

    try:
        if len(text.encode("utf-8")) > MAX_REQUEST_PACK_BYTES:
            raise ValueError("Dataset exceeds 1 MiB")
        if scorer not in {"judge", "exact"}:
            raise ValueError("Choose judge or exact scoring")
        if format == "json":
            parsed = json.loads(text.lstrip("\ufeff"), object_pairs_hook=_unique_object)
            # Reject non-finite numbers and lone surrogates before echoing raw
            # rows into the API preview, which must itself be valid UTF-8 JSON.
            try:
                json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise ValueError("JSON must contain finite numbers and valid Unicode text") from exc
            if isinstance(parsed, dict):
                try:
                    pack = RequestPack.model_validate(parsed)
                except ValidationError as exc:
                    for item in exc.errors(include_input=False, include_context=False):
                        loc = item["loc"]
                        row = loc[1] + 1 if len(loc) > 1 and loc[0] == "cases" and isinstance(loc[1], int) else None
                        field = ".".join(map(str, loc[2:] if row else loc)) or "dataset"
                        error(field, item["msg"], row)
                    return result
                result["request_pack"] = pack.model_dump(mode="json")
                result["warnings"] = pack_warnings(pack)
                result["preview_rows"] = result["request_pack"]["cases"][:MAX_DATASET_PREVIEW_ROWS]
                return result
            if not isinstance(parsed, list):
                raise ValueError("JSON must be a request pack or an array of row objects")
            rows = parsed
            columns = list(dict.fromkeys(key for row in rows if isinstance(row, dict) for key in row))
        elif format == "csv":
            # The process-wide csv field limit is left untouched. Oversize cells get
            # an actionable error instead of changing parsing limits for other users.
            reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
            columns = next(reader, [])
            if not columns or any(not name.strip() for name in columns) or len(set(columns)) != len(columns):
                error("columns", "CSV needs distinct, non-empty column names")
                return result
            rows = []
            for number, row in enumerate(reader, 1):
                if number > MAX_REQUEST_PACK_CASES:
                    raise ValueError("Dataset exceeds 1000 rows")
                if len(row) != len(columns):
                    error("row", "Row must have one value for every column", number)
                rows.append(dict(zip(columns, row)))
        else:
            raise ValueError("Choose csv or json format")
        if not rows or len(rows) > MAX_REQUEST_PACK_CASES:
            raise ValueError("Dataset must contain 1 to 1000 rows")
        result["columns"] = columns
        result["preview_rows"] = rows[:MAX_DATASET_PREVIEW_ROWS]
        for field, aliases in (("input_field", ("prompt", "question", "input", "text")),
                               ("expected_field", ("expected", "expected_answer", "answer", "reference", "output"))):
            result["suggested_mapping"][field] = next(
                (column for alias in aliases for column in columns if column.lower().strip() == alias), None)
        result["needs_mapping"] = not (input_field and expected_field)
        for field, column in (("input_field", input_field), ("expected_field", expected_field)):
            if column is not None and column not in columns:
                error(field, "Choose an existing column")
        for number, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                error("row", "Each JSON row must be an object", number)
        if result["needs_mapping"] or result["errors"]:
            return result
        cases = []
        for number, row in enumerate(rows, 1):
            valid = True
            for column in (input_field, expected_field):
                if not isinstance(row.get(column), str) or not row[column].strip():
                    error(column, "A non-empty text value is required", number)
                    valid = False
            if valid:
                cases.append({"fixture_id": f"example-{number}",
                              "messages": [{"role": "user", "content": row[input_field]}],
                              "scorer": scorer, "expected": row[expected_field]})
        if not result["errors"]:
            pack = RequestPack(version=1, cases=cases)
            result["request_pack"] = pack.model_dump(mode="json")
            result["warnings"] = pack_warnings(pack)
    except (ValueError, UnicodeError, csv.Error, RecursionError) as exc:
        message = "Invalid JSON; check the file syntax" if isinstance(exc, json.JSONDecodeError) else str(exc)
        error("dataset", message)
    return result


def try_example_config(draft: ConfigDraft, fixture_id: str, target_name: str) -> RunConfig:
    """Project a draft onto one real request using its original effective payload."""
    if draft.request_source != "request_pack" or draft.request_pack is None:
        raise ValueError("Add a dataset before trying an example")
    case = next((case for case in draft.request_pack.cases if case.fixture_id == fixture_id), None)
    target = next((target for target in draft.targets if target.name == target_name), None)
    if case is None:
        raise ValueError("Choose an existing dataset example")
    if target is None:
        raise ValueError("Choose an existing target")
    # The loaded pass owns a shared pack request. Preserve its effective
    # timeout rather than falling back to the suite default when projecting it.
    consumer = "load" if "load" in draft.checks else "capacity" if "capacity" in draft.checks else "correctness"
    effective = draft.effective_check(consumer)
    values = draft.model_dump(mode="json")
    selected_target = target.model_dump(mode="json")
    selected_target.update(parent=None, restart_command=None, metrics_url=None)
    selected_target["deployment"].update(discovery="none", collector_url=None, metadata_file=None)
    values.update(targets=[selected_target], checks=["correctness"], max_requests=1,
                  restart_target=None, custom_fixtures=None, overlap_models=False,
                  request_pack={"version": 1, "cases": [case.model_dump(mode="json")]},
                  max_output_tokens=effective.get("max_output_tokens", draft.max_output_tokens),
                  request_timeout_seconds=effective.get("request_timeout_seconds", draft.request_timeout_seconds),
                  limits=draft.effective_check("correctness")["limits"], test_options={})
    return RunConfig.model_validate(values)
