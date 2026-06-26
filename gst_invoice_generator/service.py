from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable

from .core import (
    METADATA_FILE,
    GoogleSheetReader,
    RequestConfig,
    _request_metadata,
    collect_bank_sales,
    existing_output_for_requested_months,
    format_month_list,
    missing_months_in_metadata,
    write_outputs,
)

ProgressCallback = Callable[[str], None]


def _metadata_runs(output_dir: str | Path) -> list[dict[str, Any]]:
    metadata_path = Path(output_dir) / METADATA_FILE
    if not metadata_path.is_file():
        return []
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return metadata.get("runs", []) if isinstance(metadata, dict) else []


def _excel_rows(path: str | Path) -> list[dict[str, Any]]:
    detail_path = Path(path)
    if not detail_path.is_file():
        return []
    openpyxl = __import__("openpyxl")
    workbook = openpyxl.load_workbook(detail_path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    try:
        headers = [str(value or "") for value in next(rows)]
    except StopIteration:
        workbook.close()
        return []
    data = [dict(zip(headers, row, strict=False)) for row in rows]
    workbook.close()
    return data


def _parse_excel_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _receipt_path_for(receipt_no: str, entry_date: date, output_paths: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    for receipt_dir in output_paths.get("receipt_dirs", []) or []:
        candidates.append(Path(receipt_dir) / f"{receipt_no}.pdf")
    receipts_root = output_paths.get("receipts_dir")
    if receipts_root:
        candidates.append(Path(receipts_root) / f"{entry_date.month:02d}" / "receipts" / f"{receipt_no}.pdf")
    for path in candidates:
        if path.is_file():
            return path
    return None


def processed_receipt_days(output_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Return processed receipt PDFs grouped by transaction date."""
    days: dict[str, dict[str, Any]] = {}
    for run in _metadata_runs(output_dir):
        if not isinstance(run, dict) or run.get("status") != "created":
            continue
        output_paths = run.get("output_paths", {})
        for row in _excel_rows(output_paths.get("detail_excel", "")):
            receipt_no = str(row.get("receipt_no") or "").strip()
            entry_date = _parse_excel_date(row.get("entry_date"))
            if not receipt_no or entry_date is None:
                continue
            receipt_path = _receipt_path_for(receipt_no, entry_date, output_paths)
            if receipt_path is None:
                continue
            key = entry_date.isoformat()
            day = days.setdefault(key, {"date": key, "receipt_paths": [], "transaction_count": 0, "bank_amount": 0.0})
            path_text = str(receipt_path)
            if path_text in day["receipt_paths"]:
                continue
            day["receipt_paths"].append(path_text)
            day["transaction_count"] += 1
            try:
                day["bank_amount"] = round(float(day["bank_amount"]) + float(row.get("bank_amount") or 0), 2)
            except (TypeError, ValueError):
                pass
    return days


def processed_ranges(output_dir: str | Path) -> list[dict[str, Any]]:
    """Return successful created runs from an output directory metadata log."""
    processed: list[dict[str, Any]] = []
    for run in _metadata_runs(output_dir):
        if not isinstance(run, dict) or run.get("status") != "created":
            continue
        request = run.get("request", {})
        totals = run.get("totals", {})
        processed.append({
            "year": request.get("year"),
            "start_month": request.get("start_month"),
            "end_month": request.get("end_month"),
            "start_datetime": run.get("start_datetime", ""),
            "end_datetime": run.get("end_datetime", ""),
            "created_on": run.get("created_on", ""),
            "transaction_count": totals.get("transaction_count", 0),
            "bank_amount": totals.get("bank_amount", 0),
            "output_paths": run.get("output_paths", {}),
        })
    return processed


def run_generation(config: RequestConfig, progress_callback: ProgressCallback | None = None) -> dict[str, Any]:
    """Run invoice generation without binding callers to CLI or Streamlit concerns."""
    notify = progress_callback or (lambda _message: None)
    notify(f"Starting {config.year}-{config.start_month:02d} through {config.year}-{config.end_month:02d}")
    if metadata := existing_output_for_requested_months(config):
        notify("Existing output covers the requested month range; reusing generated files without writing metadata")
        return metadata

    months_to_process = missing_months_in_metadata(config)
    skipped_months = [month for month in range(config.start_month, config.end_month + 1) if month not in months_to_process]
    if skipped_months:
        notify(f"Skipping already processed month(s): {format_month_list(skipped_months)}")
    if not months_to_process:
        notify("All requested months are already present in metadata; no Google Drive reads needed")
        return {
            "status": "existing_output",
            "request": _request_metadata(config),
            "created_on": datetime.now(UTC).isoformat(),
            "message": "All requested months are already present in metadata.",
            "output_paths": {"output_dir": config.output_dir, "metadata": str(Path(config.output_dir) / METADATA_FILE)},
        }

    notify(f"Reading Google Drive only for missing month(s): {format_month_list(months_to_process)}")
    reader = GoogleSheetReader(config.credentials_file)
    sales = collect_bank_sales(
        reader,
        config.drive_path,
        config.year,
        config.start_month,
        config.end_month,
        config.selling_address,
        progress_callback=notify,
        months=months_to_process,
    )
    notify(f"Collected {len(sales)} bank transaction(s); writing outputs")
    metadata = write_outputs(sales, config, progress_callback=notify)
    notify("Generation complete")
    return metadata
