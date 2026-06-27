from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import math
import re
import socket
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

GST_RATE = 0.05
METADATA_FILE = "generation_metadata.json"
GOOGLE_API_RETRIES = 5
GOOGLE_API_TIMEOUT_SECONDS = 120
CGST_RATE = 0.025
SGST_RATE = 0.025
IOB_PATTERN = re.compile(r"\bIOB(?:\s*[-:]\s*([0-9][0-9,]*(?:\.\d+)?))?\b", re.IGNORECASE)
PARTIAL_BANK_QUANTITY_EXCLUDED_ITEMS = {"due payment", "transport charges"}
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
SHEET_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
SALES_COLUMNS = [
    "Entry_Date", "Phone", "Customer_Name", "Address", "Item_Type", "Qty_Ordered",
    "Unit", "Rate", "Order_Value", "Paid_Amount", "Due_Amount", "Order_Ref", "Remarks",
    "Unnamed_Remarks",
]


def progress(message: str) -> None:
    print(f"[gst-invoice-generator] {message}", file=sys.stderr, flush=True)


def _execute_google_request(request: Any) -> dict[str, Any]:
    try:
        return request.execute(num_retries=GOOGLE_API_RETRIES)
    except TypeError:
        return request.execute()


@dataclasses.dataclass(frozen=True)
class BankSale:
    source_sheet: str
    entry_date: date | None
    phone: str
    customer_name: str
    billing_address: str
    selling_address: str
    item_type: str
    qty_ordered: float
    unit: str
    original_rate: float
    order_value: float
    bank_amount: float
    taxable_value: float
    cgst: float
    sgst: float
    adjusted_rate: float
    discount_before_tax: float
    order_ref: str
    remarks: str


@dataclasses.dataclass(frozen=True)
class RequestConfig:
    credentials_file: str
    drive_path: str
    year: int
    start_month: int
    end_month: int
    output_dir: str
    seller_name: str = "Seller"
    seller_gstin: str = ""
    selling_address: str = ""

    def validate(self) -> None:
        if not self.credentials_file:
            raise ValueError("credentials_file is required")
        if not self.drive_path:
            raise ValueError("drive_path is required")
        if not self.output_dir:
            raise ValueError("output_dir is required")
        if not 1 <= self.start_month <= self.end_month <= 12:
            raise ValueError("Month range must satisfy 1 <= start_month <= end_month <= 12")


def _first_config_value(value: Any, default: str = "") -> str:
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value if value is not None else default)


def load_request_config(config_file: str) -> RequestConfig:
    data = json.loads(Path(config_file).read_text(encoding="utf-8"))
    config = RequestConfig(
        credentials_file=str(data.get("credentials_file", "")),
        drive_path=str(data.get("drive_path", "")),
        year=int(data.get("year", 0)),
        start_month=int(data.get("start_month", 0)),
        end_month=int(data.get("end_month", 0)),
        output_dir=str(data.get("output_dir", "")),
        seller_name=_first_config_value(data.get("seller_name"), "Seller"),
        seller_gstin=_first_config_value(data.get("seller_gstin")),
        selling_address=_first_config_value(data.get("selling_address")),
    )
    config.validate()
    return config


def config_with_cli_overrides(config: RequestConfig, args: argparse.Namespace) -> RequestConfig:
    updates = {
        field.name: getattr(args, field.name)
        for field in dataclasses.fields(RequestConfig)
        if hasattr(args, field.name) and getattr(args, field.name) is not None
    }
    merged = dataclasses.replace(config, **updates)
    merged.validate()
    return merged


def parse_money(value: Any) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", ".", "-"}:
        return 0.0
    return float(text)


def parse_date(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def extract_iob_amount(remarks: Any, unnamed_remarks: Any, order_value: Any) -> float:
    combined = " ".join(str(v or "") for v in (remarks, unnamed_remarks))
    match = IOB_PATTERN.search(combined)
    if not match:
        return 0.0
    explicit_amount = match.group(1)
    return parse_money(explicit_amount) if explicit_amount else parse_money(order_value)


def has_explicit_iob_amount(remarks: Any, unnamed_remarks: Any) -> bool:
    combined = " ".join(str(v or "") for v in (remarks, unnamed_remarks))
    match = IOB_PATTERN.search(combined)
    return bool(match and match.group(1))


def _uses_partial_bank_quantity(row: dict[str, Any], original_rate: float) -> bool:
    item_type = str(row.get("Item_Type", "")).strip().lower()
    return (
        original_rate > 0
        and item_type not in PARTIAL_BANK_QUANTITY_EXCLUDED_ITEMS
        and has_explicit_iob_amount(row.get("Remarks"), row.get("Unnamed_Remarks"))
    )


def _partial_bank_quantity_and_discount(taxable: float, original_rate: float) -> tuple[float, float]:
    exact_qty = taxable / original_rate
    if math.isclose(exact_qty, round(exact_qty), abs_tol=0.000001):
        return round(exact_qty, 6), 0.0
    rounded_qty = math.ceil(exact_qty)
    discount = round((rounded_qty * original_rate) - taxable, 2)
    return float(rounded_qty), discount


def normalize_rows(values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    header = [str(c).strip() for c in values[0]]
    rows: list[dict[str, Any]] = []
    for raw in values[1:]:
        padded = list(raw) + [""] * max(0, len(SALES_COLUMNS) - len(raw))
        row = {SALES_COLUMNS[i]: padded[i] if i < len(padded) else "" for i in range(len(SALES_COLUMNS))}
        for idx, name in enumerate(header[: len(raw)]):
            if name:
                row[name] = raw[idx]
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    return rows


def row_to_bank_sale(row: dict[str, Any], source_sheet: str, company_selling_address: str) -> BankSale | None:
    bank_amount = extract_iob_amount(row.get("Remarks"), row.get("Unnamed_Remarks"), row.get("Order_Value"))
    if bank_amount <= 0:
        return None
    qty = parse_money(row.get("Qty_Ordered")) or 1.0
    original_rate = parse_money(row.get("Rate"))
    taxable = round(bank_amount / (1 + GST_RATE), 2)
    adjusted_rate = round(taxable / qty, 2)
    discount_before_tax = 0.0
    if _uses_partial_bank_quantity(row, original_rate):
        qty, discount_before_tax = _partial_bank_quantity_and_discount(taxable, original_rate)
        adjusted_rate = round(original_rate, 2)
    cgst = round(taxable * CGST_RATE, 2)
    sgst = round(taxable * SGST_RATE, 2)
    return BankSale(
        source_sheet=source_sheet,
        entry_date=parse_date(row.get("Entry_Date")),
        phone=str(row.get("Phone", "")).strip(),
        customer_name=str(row.get("Customer_Name", "")).strip() or "Walk-in Customer",
        billing_address=str(row.get("Address", "")).strip(),
        selling_address=company_selling_address,
        item_type=str(row.get("Item_Type", "")).strip(),
        qty_ordered=qty,
        unit=str(row.get("Unit", "")).strip(),
        original_rate=original_rate,
        order_value=parse_money(row.get("Order_Value")),
        bank_amount=round(bank_amount, 2),
        taxable_value=taxable,
        cgst=cgst,
        sgst=sgst,
        adjusted_rate=adjusted_rate,
        discount_before_tax=discount_before_tax,
        order_ref=str(row.get("Order_Ref", "")).strip(),
        remarks=" ".join(str(v or "").strip() for v in (row.get("Remarks"), row.get("Unnamed_Remarks"))).strip(),
    )


def month_folder_name(month: int) -> str:
    return f"{month:02d}_{MONTH_NAMES[month - 1]}"


def build_folder_paths(root_drive_path: str, year: int, start_month: int, end_month: int) -> list[str]:
    root = root_drive_path.strip("/")
    return [f"{root}/{year}/{month_folder_name(month)}" for month in range(start_month, end_month + 1)]


def sheet_date_from_name(sheet_name: str) -> date | None:
    match = SHEET_DATE_PATTERN.search(sheet_name)
    return parse_date(match.group(1)) if match else None


def sheet_is_in_requested_range(sheet_name: str, year: int, start_month: int, end_month: int) -> bool:
    sheet_date = sheet_date_from_name(sheet_name)
    if sheet_date is None:
        return True
    return sheet_date.year == year and start_month <= sheet_date.month <= end_month


class GoogleSheetReader:
    def __init__(self, credentials_file: str):
        socket.setdefaulttimeout(GOOGLE_API_TIMEOUT_SECONDS)
        service_account = importlib.import_module("google.oauth2.service_account")
        discovery = importlib.import_module("googleapiclient.discovery")
        scopes = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/spreadsheets.readonly"]
        credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)
        self.drive = discovery.build("drive", "v3", credentials=credentials)
        self.sheets = discovery.build("sheets", "v4", credentials=credentials)

    def _find_child(self, parent_id: str | None, name: str, mime_type: str | None = None) -> str:
        clauses = [f"name = '{name.replace("'", "\\'")}'", "trashed = false"]
        if parent_id:
            clauses.append(f"'{parent_id}' in parents")
        if mime_type:
            clauses.append(f"mimeType = '{mime_type}'")
        result = self.drive.files().list(
            q=" and ".join(clauses),
            fields="files(id, name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        )
        result = _execute_google_request(result)
        files = result.get("files", [])
        if not files:
            raise FileNotFoundError(f"Could not find Drive item: {name}")
        return files[0]["id"]

    def folder_id_for_path(self, drive_path: str) -> str:
        parent_id: str | None = None
        path_parts = [p for p in drive_path.strip("/").split("/") if p]
        resolved_parts: list[str] = []
        for part in path_parts:
            resolved_parts.append(part)
            try:
                parent_id = self._find_child(parent_id, part, "application/vnd.google-apps.folder")
            except FileNotFoundError as exc:
                resolved_path = "/".join(resolved_parts)
                raise FileNotFoundError(
                    f"Could not find Drive folder path: {resolved_path}. "
                    "Verify the folder name and that it is shared with the service account."
                ) from exc
        if parent_id is None:
            raise ValueError("Drive path must include at least one folder name")
        return parent_id

    def spreadsheet_ids_in_folder(self, folder_id: str) -> list[tuple[str, str]]:
        q = f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
        request = self.drive.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        )
        result = _execute_google_request(request)
        return sorted((f["id"], f["name"]) for f in result.get("files", []))

    def read_sales_values(self, spreadsheet_id: str) -> list[list[Any]]:
        request = self.sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range="SALES_ENTRY!A:N")
        result = _execute_google_request(request)
        return result.get("values", [])


def collect_bank_sales(reader: GoogleSheetReader, drive_path: str, year: int, start_month: int, end_month: int, selling_address: str, progress_callback: Any | None = None, months: Iterable[int] | None = None) -> list[BankSale]:
    notify = progress_callback or progress
    sales: list[BankSale] = []
    selected_months = list(months) if months is not None else list(range(start_month, end_month + 1))
    for month in selected_months:
        folder_path = build_folder_paths(drive_path, year, month, month)[0]
        notify(f"Opening Drive folder: {folder_path}")
        folder_id = reader.folder_id_for_path(folder_path)
        spreadsheets = reader.spreadsheet_ids_in_folder(folder_id)
        notify(f"Found {len(spreadsheets)} spreadsheet(s) in {folder_path}")
        for spreadsheet_id, sheet_name in spreadsheets:
            if not sheet_is_in_requested_range(sheet_name, year, start_month, end_month):
                notify(f"Skipping outside requested range: {sheet_name}")
                continue
            notify(f"Reading SALES_ENTRY from: {sheet_name}")
            rows = normalize_rows(reader.read_sales_values(spreadsheet_id))
            sheet_sales = [s for row in rows if (s := row_to_bank_sale(row, sheet_name, selling_address))]
            sales.extend(sheet_sales)
            notify(f"Collected {len(sheet_sales)} bank transaction(s) from {sheet_name}; running total: {len(sales)}")
    return sales


def _money(value: float) -> str:
    return f"Rs. {value:,.2f}"


def _para_text(value: Any) -> str:
    return escape(str(value or ""))


def _receipt_pdf(path: Path, sale: BankSale, receipt_no: str, seller_name: str, seller_gstin: str) -> None:
    colors = importlib.import_module("reportlab.lib.colors")
    pagesizes = importlib.import_module("reportlab.lib.pagesizes")
    styles_mod = importlib.import_module("reportlab.lib.styles")
    units = importlib.import_module("reportlab.lib.units")
    platypus = importlib.import_module("reportlab.platypus")

    doc = platypus.SimpleDocTemplate(
        str(path),
        pagesize=pagesizes.A4,
        rightMargin=18 * units.mm,
        leftMargin=18 * units.mm,
        topMargin=16 * units.mm,
        bottomMargin=16 * units.mm,
        title=f"Sale Receipt {receipt_no}",
        author=seller_name,
    )
    styles = styles_mod.getSampleStyleSheet()
    normal = styles["Normal"]
    title = styles["Title"]
    title.textColor = colors.HexColor("#12355B")
    small = styles_mod.ParagraphStyle("Small", parent=normal, fontSize=8, leading=10, textColor=colors.HexColor("#5D6975"))
    story: list[Any] = []

    story.append(platypus.Table(
        [[platypus.Paragraph(f"<b>{_para_text(seller_name)}</b><br/>{_para_text(sale.selling_address)}<br/>GSTIN: {_para_text(seller_gstin) or '-'}", normal),
          platypus.Paragraph(f"<b>Tax Invoice / Sale Receipt</b><br/>Receipt No: {_para_text(receipt_no)}<br/>Date: {_para_text(sale.entry_date.isoformat() if sale.entry_date else '-')}<br/>Payment: Bank / IOB", normal)]],
        colWidths=[105 * units.mm, 65 * units.mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F7FB")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#B8C7D9")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 10),
        ],
    ))
    story.append(platypus.Spacer(1, 10))
    story.append(platypus.Paragraph("Bill To", title))
    story.append(platypus.Paragraph(f"<b>{_para_text(sale.customer_name)}</b><br/>{_para_text(sale.billing_address) or '-'}<br/>Phone: {_para_text(sale.phone) or '-'}", normal))
    story.append(platypus.Spacer(1, 12))

    line_items = [
        ["Item", "Qty", "Unit", "Taxable Rate", "Taxable Value", "CGST 2.5%", "SGST 2.5%", "Total"],
        [_para_text(sale.item_type) or "-", f"{sale.qty_ordered:g}", _para_text(sale.unit) or "-", _money(sale.adjusted_rate),
         _money(sale.taxable_value), _money(sale.cgst), _money(sale.sgst), _money(sale.bank_amount)],
    ]
    story.append(platypus.Table(
        line_items,
        repeatRows=1,
        colWidths=[42 * units.mm, 14 * units.mm, 14 * units.mm, 24 * units.mm, 25 * units.mm, 21 * units.mm, 21 * units.mm, 25 * units.mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12355B")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE8")),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ],
    ))
    story.append(platypus.Spacer(1, 10))
    story.append(platypus.Table(
        [["Taxable Value", _money(sale.taxable_value)], ["Discount Before Tax", _money(sale.discount_before_tax)], ["CGST", _money(sale.cgst)], ["SGST", _money(sale.sgst)], ["Grand Total", _money(sale.bank_amount)]],
        colWidths=[35 * units.mm, 35 * units.mm],
        hAlign="RIGHT",
        style=[
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE8")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E9F2FF")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ],
    ))
    story.append(platypus.Spacer(1, 12))
    story.append(platypus.Paragraph(f"Source: {_para_text(sale.source_sheet)} | Order Ref: {_para_text(sale.order_ref) or '-'} | Remarks: {_para_text(sale.remarks) or '-'}", small))
    story.append(platypus.Paragraph("This is a computer-generated receipt.", small))
    doc.build(story)


def _write_xlsx(path: Path, headers: list[str], data_rows: list[list[Any]]) -> None:
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in data_rows:
        sheet.append(row)
    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 48)
    workbook.save(path)


def _append_xlsx(path: Path, headers: list[str], data_rows: list[list[Any]]) -> None:
    openpyxl = importlib.import_module("openpyxl")
    if path.is_file():
        workbook = openpyxl.load_workbook(path)
        sheet = workbook.active
        if sheet.max_row == 0:
            sheet.append(headers)
    else:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
    for row in data_rows:
        sheet.append(row)
    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 48)
    workbook.save(path)


def _receipt_number_value(receipt_no: Any) -> int:
    match = re.search(r"(\d+)$", str(receipt_no or ""))
    return int(match.group(1)) if match else 0


def _next_receipt_index(detail_excel: Path) -> int:
    existing_rows = _read_detail_rows(detail_excel)
    if not existing_rows:
        return 1
    return max(_receipt_number_value(row.get("receipt_no")) for row in existing_rows) + 1


def _actual_transaction_window(rows: list[dict[str, Any]]) -> tuple[str, str]:
    entry_dates = sorted(row["entry_date"] for row in rows if row.get("entry_date"))
    if not entry_dates:
        return "", ""
    start = datetime.combine(entry_dates[0], time.min).isoformat()
    end = datetime.combine(entry_dates[-1], time.max).replace(microsecond=0).isoformat()
    return start, end


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "transaction_count": len(rows),
        "bank_amount": round(sum(row["bank_amount"] for row in rows), 2),
        "taxable_value": round(sum(row["taxable_value"] for row in rows), 2),
        "cgst": round(sum(row["cgst"] for row in rows), 2),
        "sgst": round(sum(row["sgst"] for row in rows), 2),
    }



def _month_key(row: dict[str, Any]) -> int | None:
    entry_date = row.get("entry_date")
    if isinstance(entry_date, datetime):
        entry_date = entry_date.date()
    if isinstance(entry_date, date):
        return entry_date.month
    parsed = parse_date(entry_date)
    return parsed.month if parsed else None


def _monthly_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for month in sorted({month for row in rows if (month := _month_key(row)) is not None}):
        month_rows = [row for row in rows if _month_key(row) == month]
        start_datetime, end_datetime = _actual_transaction_window(month_rows)
        summaries.append({
            "year": month_rows[0].get("entry_date").year if isinstance(month_rows[0].get("entry_date"), date) else None,
            "month": month,
            "month_name": MONTH_NAMES[month - 1],
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            **_totals(month_rows),
        })
    return summaries


def _summary_table_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    table_rows: list[list[Any]] = []
    for summary in _monthly_summaries(rows):
        table_rows.append([
            f"{summary['month']:02d}-{summary['month_name']}",
            summary["start_datetime"],
            summary["end_datetime"],
            summary["transaction_count"],
            summary["bank_amount"],
            summary["taxable_value"],
            summary["cgst"],
            summary["sgst"],
        ])
    return table_rows


def _read_detail_rows(path: str | Path) -> list[dict[str, Any]]:
    detail_path = Path(path)
    if not detail_path.is_file():
        return []
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(detail_path, read_only=True, data_only=True)
    sheet = workbook.active
    values = sheet.iter_rows(values_only=True)
    try:
        headers = [str(value or "") for value in next(values)]
    except StopIteration:
        workbook.close()
        return []
    rows = [dict(zip(headers, row, strict=False)) for row in values]
    workbook.close()
    return rows


def _numeric_row(row: dict[str, Any]) -> dict[str, Any]:
    converted = dict(row)
    converted["entry_date"] = parse_date(row.get("entry_date"))
    for key in ("bank_amount", "taxable_value", "cgst", "sgst"):
        converted[key] = parse_money(row.get(key))
    return converted


def _metadata_for_requested_months(config: RequestConfig, source_metadata: dict[str, Any]) -> dict[str, Any]:
    requested_months = set(range(config.start_month, config.end_month + 1))
    rows = [
        row
        for row in (_numeric_row(raw) for raw in _read_detail_rows(source_metadata.get("output_paths", {}).get("detail_excel", "")))
        if row.get("entry_date") and row["entry_date"].year == config.year and row["entry_date"].month in requested_months
    ]
    start_datetime, end_datetime = _actual_transaction_window(rows)
    return {
        "status": "existing_output",
        "request": _request_metadata(config),
        "created_on": datetime.now(UTC).isoformat(),
        "reused_run_created_on": source_metadata.get("created_on", ""),
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "totals": _totals(rows),
        "month_summaries": [summary for summary in _monthly_summaries(rows) if summary["month"] in requested_months],
        "output_paths": source_metadata.get("output_paths", {}),
    }


def existing_output_for_requested_months(config: RequestConfig) -> dict[str, Any] | None:
    created_runs = _created_metadata_for_drive_year(config)
    for metadata in reversed(created_runs):
        request = metadata.get("request", {})
        try:
            start_month = int(request.get("start_month", 0))
            end_month = int(request.get("end_month", 0))
        except (TypeError, ValueError):
            continue
        if start_month <= config.start_month <= config.end_month <= end_month:
            return _metadata_for_requested_months(config, metadata)
    if created_runs and not missing_months_in_metadata(config):
        return _metadata_for_requested_months(config, created_runs[-1])
    return None


def _request_metadata(config: RequestConfig) -> dict[str, Any]:
    return {
        "drive_path": config.drive_path,
        "year": config.year,
        "start_month": config.start_month,
        "end_month": config.end_month,
    }


def _metadata_path(config: RequestConfig) -> Path:
    return Path(config.output_dir) / METADATA_FILE


def _metadata_log(config: RequestConfig) -> dict[str, Any]:
    metadata_path = _metadata_path(config)
    if not metadata_path.exists():
        return {"runs": []}
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if isinstance(metadata, dict) and isinstance(metadata.get("runs"), list):
        return metadata
    if isinstance(metadata, dict) and "request" in metadata:
        return {"runs": [metadata]}
    return {"runs": []}


def _append_metadata_run(config: RequestConfig, run_metadata: dict[str, Any]) -> dict[str, Any]:
    metadata_path = _metadata_path(config)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    log = _metadata_log(config)
    log["runs"].append(run_metadata)
    metadata_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return run_metadata


def existing_output_metadata(config: RequestConfig) -> dict[str, Any] | None:
    for metadata in reversed(_metadata_log(config)["runs"]):
        if metadata.get("status") not in {None, "created"}:
            continue
        if metadata.get("request") != _request_metadata(config):
            continue
        output_paths = metadata.get("output_paths", {})
        required = [output_paths.get("summary_excel"), output_paths.get("detail_excel"), output_paths.get("receipts_dir")]
        if all(path and Path(path).exists() for path in required):
            return metadata
    return None



def _created_metadata_for_drive_year(config: RequestConfig) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for metadata in _metadata_log(config)["runs"]:
        if metadata.get("status") not in {None, "created"}:
            continue
        request = metadata.get("request", {})
        if request.get("drive_path") != config.drive_path or request.get("year") != config.year:
            continue
        output_paths = metadata.get("output_paths", {})
        required = [output_paths.get("summary_excel"), output_paths.get("detail_excel"), output_paths.get("receipts_dir")]
        if all(path and Path(path).exists() for path in required):
            runs.append(metadata)
    return runs


def existing_months_in_metadata(config: RequestConfig) -> set[int]:
    """Return requested months already covered by successful metadata for this Drive path/year."""
    months: set[int] = set()
    requested = set(range(config.start_month, config.end_month + 1))
    for metadata in _created_metadata_for_drive_year(config):
        request = metadata.get("request", {})
        try:
            start_month = int(request.get("start_month", 0))
            end_month = int(request.get("end_month", 0))
        except (TypeError, ValueError):
            continue
        months.update(month for month in range(start_month, end_month + 1) if month in requested)
    return months


def missing_months_in_metadata(config: RequestConfig) -> list[int]:
    existing = existing_months_in_metadata(config)
    return [month for month in range(config.start_month, config.end_month + 1) if month not in existing]


def format_month_list(months: Iterable[int]) -> str:
    return ", ".join(f"{month:02d}-{MONTH_NAMES[month - 1]}" for month in months)


def _receipt_month(sale: BankSale, config: RequestConfig) -> str:
    month = sale.entry_date.month if sale.entry_date else config.start_month
    return f"{month:02d}"


def write_outputs(sales: Iterable[BankSale], config: RequestConfig, progress_callback: Any | None = None) -> dict[str, Any]:
    notify = progress_callback or progress
    seller_name = config.seller_name
    seller_gstin = config.seller_gstin
    output_dir = config.output_dir
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    receipts_root = out
    rows = [dataclasses.asdict(s) for s in sales]
    notify(f"Writing {len(rows)} receipt PDF(s) under monthly folders in {receipts_root}")
    detail_excel = out / "bank_transactions_detailed.xlsx"
    summary_excel = out / "bank_transactions_summary.xlsx"
    next_receipt_index = _next_receipt_index(detail_excel)
    for sequence, row in enumerate(rows, start=1):
        receipt_index = next_receipt_index + sequence - 1
        sale = BankSale(**row)
        receipt_no = f"BANK-{receipt_index:05d}"
        row["receipt_no"] = receipt_no
        receipts_dir = receipts_root / _receipt_month(sale, config) / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        _receipt_pdf(receipts_dir / f"{receipt_no}.pdf", sale, receipt_no, seller_name, seller_gstin)
        if sequence == 1 or sequence == len(rows) or sequence % 10 == 0:
            notify(f"Wrote {sequence}/{len(rows)} receipt PDF(s)")
    headers = ["receipt_no", *[f.name for f in dataclasses.fields(BankSale)]]
    detail_rows = [[row.get(header, "") for header in headers] for row in rows]
    notify(f"Appending detailed Excel report: {detail_excel}")
    _append_xlsx(detail_excel, headers, detail_rows)
    start_datetime, end_datetime = _actual_transaction_window(rows)
    totals = _totals(rows)
    summary_rows = _summary_table_rows(rows)
    notify(f"Appending summary Excel report: {summary_excel}")
    _append_xlsx(summary_excel, ["period", "start_datetime", "end_datetime", "transaction_count", "bank_amount", "taxable_value", "cgst", "sgst"], summary_rows)
    metadata = {
        "status": "created",
        "request": _request_metadata(config),
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "created_on": datetime.now(UTC).isoformat(),
        "totals": totals,
        "month_summaries": _monthly_summaries(rows),
        "output_paths": {
            "output_dir": str(out),
            "receipts_dir": str(receipts_root),
            "receipt_dirs": sorted(str(path) for path in receipts_root.glob("*/receipts") if path.is_dir()),
            "detail_excel": str(detail_excel),
            "summary_excel": str(summary_excel),
            "metadata": str(out / METADATA_FILE),
        },
    }
    notify(f"Appending run metadata: {out / METADATA_FILE}")
    return _append_metadata_run(config, metadata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate GST bank sale receipts from Google Sheets SALES_ENTRY tabs.")
    parser.add_argument("--config-file", required=True, help="Path to a JSON request configuration file.")
    parser.add_argument("--credentials-file", help="Override credentials_file from config.")
    parser.add_argument("--drive-path", help="Override drive_path from config.")
    parser.add_argument("--year", type=int, help="Override year from config.")
    parser.add_argument("--start-month", type=int, help="Override start_month from config.")
    parser.add_argument("--end-month", type=int, help="Override end_month from config.")
    parser.add_argument("--output-dir", help="Override output_dir from config.")
    parser.add_argument("--seller-name", help="Override seller_name from config.")
    parser.add_argument("--seller-gstin", help="Override seller_gstin from config.")
    parser.add_argument("--selling-address", help="Override selling_address from config.")
    args = parser.parse_args(argv)
    config = config_with_cli_overrides(load_request_config(args.config_file), args)
    progress(f"Starting bank receipt generation for {config.year}-{config.start_month:02d} through {config.year}-{config.end_month:02d}")
    if metadata := existing_output_for_requested_months(config):
        progress("Existing output covers the requested month range; skipping Google Drive reads without writing metadata")
        print(json.dumps({
            "status": "existing_output",
            "message": "Output already covers this requested month range. Reusing the previous run.",
            "bank_transactions": metadata.get("totals", {}).get("transaction_count", 0),
            "bank_amount": metadata.get("totals", {}).get("bank_amount", 0),
            "output_dir": metadata.get("output_paths", {}).get("output_dir", config.output_dir),
            "metadata": metadata.get("output_paths", {}).get("metadata", str(Path(config.output_dir) / METADATA_FILE)),
        }, indent=2))
        return 0
    months_to_process = missing_months_in_metadata(config)
    skipped_months = [month for month in range(config.start_month, config.end_month + 1) if month not in months_to_process]
    if skipped_months:
        progress(f"Skipping already processed month(s): {format_month_list(skipped_months)}")
    if not months_to_process:
        progress("All requested months are already present in metadata; no Google Drive reads needed")
        print(json.dumps({
            "status": "existing_output",
            "message": "All requested months are already present in metadata.",
            "output_dir": config.output_dir,
            "metadata": str(Path(config.output_dir) / METADATA_FILE),
        }, indent=2))
        return 0
    progress(f"Reading from Google Drive only for missing month(s): {format_month_list(months_to_process)}")
    reader = GoogleSheetReader(config.credentials_file)
    sales = collect_bank_sales(reader, config.drive_path, config.year, config.start_month, config.end_month, config.selling_address, months=months_to_process)
    progress(f"Finished reading Google Sheets; collected {len(sales)} bank transaction(s)")
    metadata = write_outputs(sales, config)
    print(json.dumps({
        "status": "created",
        "bank_transactions": metadata["totals"]["transaction_count"],
        "output_dir": metadata["output_paths"]["output_dir"],
        "metadata": metadata["output_paths"]["metadata"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
