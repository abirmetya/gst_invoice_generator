from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .core import (
    METADATA_FILE,
    GoogleSheetReader,
    RequestConfig,
    _append_metadata_run,
    _request_metadata,
    collect_bank_sales,
    existing_output_metadata,
    write_outputs,
)

ProgressCallback = Callable[[str], None]


def processed_ranges(output_dir: str | Path) -> list[dict[str, Any]]:
    """Return successful created runs from an output directory metadata log."""
    metadata_path = Path(output_dir) / METADATA_FILE
    if not metadata_path.exists():
        return []
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    runs = metadata.get("runs", []) if isinstance(metadata, dict) else []
    processed: list[dict[str, Any]] = []
    for run in runs:
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
    if metadata := existing_output_metadata(config):
        notify("Existing output found; reusing generated files")
        reuse_metadata = {
            "status": "existing_output",
            "request": _request_metadata(config),
            "created_on": datetime.now(UTC).isoformat(),
            "reused_run_created_on": metadata.get("created_on", ""),
            "start_datetime": metadata.get("start_datetime", ""),
            "end_datetime": metadata.get("end_datetime", ""),
            "totals": metadata.get("totals", {}),
            "output_paths": metadata.get("output_paths", {}),
        }
        _append_metadata_run(config, reuse_metadata)
        return reuse_metadata

    notify("No matching output found; reading Google Drive")
    reader = GoogleSheetReader(config.credentials_file)
    sales = collect_bank_sales(
        reader,
        config.drive_path,
        config.year,
        config.start_month,
        config.end_month,
        config.selling_address,
        progress_callback=notify,
    )
    notify(f"Collected {len(sales)} bank transaction(s); writing outputs")
    metadata = write_outputs(sales, config, progress_callback=notify)
    notify("Generation complete")
    return metadata
