import argparse
import json

import pytest

from gst_invoice_generator.core import (
    GoogleSheetReader,
    RequestConfig,
    _append_metadata_run,
    build_folder_paths,
    config_with_cli_overrides,
    existing_output_metadata,
    load_request_config,
    sheet_is_in_requested_range,
    extract_iob_amount,
    normalize_rows,
    row_to_bank_sale,
    write_outputs,
)


class FakeDriveFiles:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)

        class Request:
            def execute(self):
                return response

        return Request()


class FakeDrive:
    def __init__(self, responses):
        self.fake_files = FakeDriveFiles(responses)

    def files(self):
        return self.fake_files


def test_extract_iob_amount_full_order_value():
    assert extract_iob_amount("IOB", "", "1,050") == 1050.0


def test_extract_iob_amount_partial_from_unnamed_column():
    assert extract_iob_amount("cash", "IOB-525", "1050") == 525.0


def test_row_to_bank_sale_treats_bank_amount_as_gst_inclusive():
    row = {
        "Entry_Date": "2026-06-24",
        "Phone": "9999999999",
        "Customer_Name": "Acme",
        "Address": "Billing Addr",
        "Item_Type": "Milk",
        "Qty_Ordered": "10",
        "Unit": "L",
        "Rate": "105",
        "Order_Value": "1050",
        "Order_Ref": "ORD-1",
        "Remarks": "IOB",
        "Unnamed_Remarks": "",
    }
    sale = row_to_bank_sale(row, "Daily_Operations_2026-06-24", "Shop Addr")
    assert sale is not None
    assert sale.bank_amount == 1050.0
    assert sale.taxable_value == 1000.0
    assert sale.cgst == 25.0
    assert sale.sgst == 25.0
    assert sale.adjusted_rate == 100.0


def test_build_folder_paths():
    assert build_folder_paths("Google_Business_Data/Daily_Operation", 2026, 1, 2) == [
        "Google_Business_Data/Daily_Operation/2026/01_January",
        "Google_Business_Data/Daily_Operation/2026/02_February",
    ]


def test_normalize_rows_includes_unnamed_column_after_remarks():
    rows = normalize_rows([
        ["Entry_Date", "Phone", "Customer_Name", "Address", "Item_Type", "Qty_Ordered", "Unit", "Rate", "Order_Value", "Paid_Amount", "Due_Amount", "Order_Ref", "Remarks", ""],
        ["2026-06-24", "1", "Name", "Addr", "Item", "1", "kg", "10", "10", "10", "0", "R", "cash", "IOB-10"],
    ])
    assert rows[0]["Unnamed_Remarks"] == "IOB-10"


def test_sheet_date_filter_keeps_only_requested_year_and_months():
    assert sheet_is_in_requested_range("Daily_Operations_2026-04-15", 2026, 4, 6)
    assert not sheet_is_in_requested_range("Daily_Operations_2025-04-15", 2026, 4, 6)
    assert not sheet_is_in_requested_range("Daily_Operations_2026-07-01", 2026, 4, 6)
    assert sheet_is_in_requested_range("Manual_Adjustment", 2026, 4, 6)


def test_load_request_config_from_json(tmp_path):
    config_file = tmp_path / "request_config.json"
    config_file.write_text(json.dumps({
        "credentials_file": "/secure/service-account.json",
        "drive_path": "Google_Business_Data/Daily_Operation",
        "year": 2026,
        "start_month": 4,
        "end_month": 6,
        "output_dir": "./outputs/apr-jun-2026",
        "seller_name": "Shop",
        "seller_gstin": "GSTIN",
        "selling_address": "Address",
    }))

    config = load_request_config(str(config_file))

    assert config.drive_path == "Google_Business_Data/Daily_Operation"
    assert config.year == 2026
    assert config.start_month == 4
    assert config.end_month == 6
    assert config.seller_name == "Shop"


def test_config_with_cli_overrides_only_replaces_provided_values():
    config = RequestConfig(
        credentials_file="creds.json",
        drive_path="Google_Business_Data/Daily_Operation",
        year=2026,
        start_month=4,
        end_month=6,
        output_dir="./outputs",
        seller_name="Original",
    )
    args = argparse.Namespace(
        credentials_file=None,
        drive_path=None,
        year=None,
        start_month=5,
        end_month=5,
        output_dir=None,
        seller_name="Override",
        seller_gstin=None,
        selling_address=None,
    )

    merged = config_with_cli_overrides(config, args)

    assert merged.start_month == 5
    assert merged.end_month == 5
    assert merged.seller_name == "Override"
    assert merged.drive_path == "Google_Business_Data/Daily_Operation"


def test_folder_lookup_searches_shared_drives():
    reader = GoogleSheetReader.__new__(GoogleSheetReader)
    reader.drive = FakeDrive([
        {"files": [{"id": "root-folder", "name": "Google_Business_Data"}]},
        {"files": [{"id": "daily-folder", "name": "Daily_Operation"}]},
    ])

    assert reader.folder_id_for_path("Google_Business_Data/Daily_Operation") == "daily-folder"

    first_call = reader.drive.fake_files.calls[0]
    assert first_call["supportsAllDrives"] is True
    assert first_call["includeItemsFromAllDrives"] is True
    assert first_call["corpora"] == "allDrives"


def test_folder_lookup_error_includes_path_and_share_hint():
    reader = GoogleSheetReader.__new__(GoogleSheetReader)
    reader.drive = FakeDrive([{"files": []}])

    try:
        reader.folder_id_for_path("Google_Business_Data/Daily_Operation")
    except FileNotFoundError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected FileNotFoundError")

    assert "Could not find Drive folder path: Google_Business_Data" in message
    assert "shared with the service account" in message


def test_spreadsheet_lookup_searches_shared_drives():
    reader = GoogleSheetReader.__new__(GoogleSheetReader)
    reader.drive = FakeDrive([{"files": [{"id": "sheet-1", "name": "Daily_Operations_2026-06-24"}]}])

    assert reader.spreadsheet_ids_in_folder("folder-1") == [("sheet-1", "Daily_Operations_2026-06-24")]
    call = reader.drive.fake_files.calls[0]
    assert call["supportsAllDrives"] is True
    assert call["includeItemsFromAllDrives"] is True
    assert call["corpora"] == "allDrives"


def test_write_outputs_creates_pdf_receipts_summary_datetime_and_metadata(tmp_path):
    pytest.importorskip("reportlab")
    row = {
        "Entry_Date": "2026-06-24",
        "Phone": "9999999999",
        "Customer_Name": "Acme",
        "Address": "Billing Addr",
        "Item_Type": "Milk",
        "Qty_Ordered": "10",
        "Unit": "L",
        "Rate": "105",
        "Order_Value": "1050",
        "Order_Ref": "ORD-1",
        "Remarks": "IOB",
        "Unnamed_Remarks": "",
    }
    sale = row_to_bank_sale(row, "Daily_Operations_2026-06-24", "Shop Addr")
    config = RequestConfig(
        credentials_file="creds.json",
        drive_path="Google_Business_Data/Daily_Operation",
        year=2026,
        start_month=6,
        end_month=6,
        output_dir=str(tmp_path),
        seller_name="Shop",
        seller_gstin="GSTIN",
        selling_address="Shop Addr",
    )

    metadata = write_outputs([sale], config)

    assert (tmp_path / "06" / "receipts" / "BANK-00001.pdf").exists()
    assert (tmp_path / "bank_transactions_summary.xlsx").exists()
    assert (tmp_path / "generation_metadata.json").exists()
    assert metadata["start_datetime"] == "2026-06-24T00:00:00"
    assert metadata["end_datetime"] == "2026-06-24T23:59:59"
    assert metadata["totals"]["transaction_count"] == 1
    assert metadata["output_paths"]["receipts_dir"] == str(tmp_path)
    assert metadata["output_paths"]["receipt_dirs"] == [str(tmp_path / "06" / "receipts")]
    assert existing_output_metadata(config)["output_paths"]["output_dir"] == str(tmp_path)
    log = json.loads((tmp_path / "generation_metadata.json").read_text(encoding="utf-8"))
    assert len(log["runs"]) == 1

    reused = {
        "status": "existing_output",
        "request": {
            "drive_path": config.drive_path,
            "year": config.year,
            "start_month": config.start_month,
            "end_month": config.end_month,
        },
        "created_on": "2026-06-26T00:00:00+00:00",
        "output_paths": metadata["output_paths"],
    }
    _append_metadata_run(config, reused)
    log = json.loads((tmp_path / "generation_metadata.json").read_text(encoding="utf-8"))
    assert [run["status"] for run in log["runs"]] == ["created", "existing_output"]


def test_processed_ranges_reads_created_runs(tmp_path):
    from gst_invoice_generator.service import processed_ranges

    metadata_file = tmp_path / "generation_metadata.json"
    metadata_file.write_text(json.dumps({
        "runs": [
            {
                "status": "created",
                "request": {"year": 2026, "start_month": 4, "end_month": 6},
                "start_datetime": "2026-04-01T00:00:00",
                "end_datetime": "2026-06-30T23:59:59",
                "created_on": "2026-06-26T00:00:00+00:00",
                "totals": {"transaction_count": 3, "bank_amount": 1200.0},
                "output_paths": {},
            },
            {"status": "existing_output", "request": {"year": 2026, "start_month": 4, "end_month": 6}},
        ]
    }), encoding="utf-8")

    ranges = processed_ranges(tmp_path)

    assert len(ranges) == 1
    assert ranges[0]["year"] == 2026
    assert ranges[0]["transaction_count"] == 3
