from __future__ import annotations

import argparse
import dataclasses
import html
import importlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

GST_RATE = 0.05
CGST_RATE = 0.025
SGST_RATE = 0.025
IOB_PATTERN = re.compile(r"\bIOB(?:\s*[-:]\s*([0-9][0-9,]*(?:\.\d+)?))?\b", re.IGNORECASE)
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


def load_request_config(config_file: str) -> RequestConfig:
    data = json.loads(Path(config_file).read_text(encoding="utf-8"))
    config = RequestConfig(
        credentials_file=str(data.get("credentials_file", "")),
        drive_path=str(data.get("drive_path", "")),
        year=int(data.get("year", 0)),
        start_month=int(data.get("start_month", 0)),
        end_month=int(data.get("end_month", 0)),
        output_dir=str(data.get("output_dir", "")),
        seller_name=str(data.get("seller_name", "Seller")),
        seller_gstin=str(data.get("seller_gstin", "")),
        selling_address=str(data.get("selling_address", "")),
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
    taxable = round(bank_amount / (1 + GST_RATE), 2)
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
        original_rate=parse_money(row.get("Rate")),
        order_value=parse_money(row.get("Order_Value")),
        bank_amount=round(bank_amount, 2),
        taxable_value=taxable,
        cgst=cgst,
        sgst=sgst,
        adjusted_rate=round(taxable / qty, 2),
        order_ref=str(row.get("Order_Ref", "")).strip(),
        remarks=" ".join(str(v or "").strip() for v in (row.get("Remarks"), row.get("Unnamed_Remarks"))).strip(),
    )


def month_folder_name(month: int) -> str:
    return f"{month:02d}-{MONTH_NAMES[month - 1]}"


def build_folder_paths(root_drive_path: str, year: int, start_month: int, end_month: int) -> list[str]:
    root = root_drive_path.strip("/")
    return [f"{root}/{month_folder_name(month)}" for month in range(start_month, end_month + 1)]


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
        ).execute()
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
        result = self.drive.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()
        return sorted((f["id"], f["name"]) for f in result.get("files", []))

    def read_sales_values(self, spreadsheet_id: str) -> list[list[Any]]:
        result = self.sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range="SALES_ENTRY!A:N").execute()
        return result.get("values", [])


def collect_bank_sales(reader: GoogleSheetReader, drive_path: str, year: int, start_month: int, end_month: int, selling_address: str) -> list[BankSale]:
    sales: list[BankSale] = []
    for folder_path in build_folder_paths(drive_path, year, start_month, end_month):
        folder_id = reader.folder_id_for_path(folder_path)
        for spreadsheet_id, sheet_name in reader.spreadsheet_ids_in_folder(folder_id):
            if not sheet_is_in_requested_range(sheet_name, year, start_month, end_month):
                continue
            rows = normalize_rows(reader.read_sales_values(spreadsheet_id))
            sales.extend(s for row in rows if (s := row_to_bank_sale(row, sheet_name, selling_address)))
    return sales


def receipt_html(sale: BankSale, receipt_no: str, seller_name: str, seller_gstin: str) -> str:
    invoice_date = sale.entry_date.isoformat() if sale.entry_date else ""
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Receipt {html.escape(receipt_no)}</title>
<style>body{{font-family:Arial,sans-serif;margin:32px;color:#222}}.top{{display:flex;justify-content:space-between}}table{{width:100%;border-collapse:collapse;margin-top:24px}}td,th{{border:1px solid #888;padding:8px;text-align:left}}.right{{text-align:right}}.muted{{color:#666}}</style></head>
<body><div class=\"top\"><div><h1>Tax Invoice / Sale Receipt</h1><strong>{html.escape(seller_name)}</strong><br>{html.escape(sale.selling_address)}<br>GSTIN: {html.escape(seller_gstin)}</div><div><strong>Receipt No:</strong> {html.escape(receipt_no)}<br><strong>Date:</strong> {invoice_date}<br><strong>Payment:</strong> Bank / IOB</div></div>
<h3>Bill To</h3><p><strong>{html.escape(sale.customer_name)}</strong><br>{html.escape(sale.billing_address)}<br>Phone: {html.escape(sale.phone)}</p>
<table><thead><tr><th>Item</th><th>Qty</th><th>Unit</th><th class=\"right\">Taxable Rate</th><th class=\"right\">Taxable Value</th><th class=\"right\">CGST 2.5%</th><th class=\"right\">SGST 2.5%</th><th class=\"right\">Total</th></tr></thead>
<tbody><tr><td>{html.escape(sale.item_type)}</td><td>{sale.qty_ordered:g}</td><td>{html.escape(sale.unit)}</td><td class=\"right\">{sale.adjusted_rate:.2f}</td><td class=\"right\">{sale.taxable_value:.2f}</td><td class=\"right\">{sale.cgst:.2f}</td><td class=\"right\">{sale.sgst:.2f}</td><td class=\"right\">{sale.bank_amount:.2f}</td></tr></tbody></table>
<p class=\"muted\">Source: {html.escape(sale.source_sheet)} | Order Ref: {html.escape(sale.order_ref)} | Remarks: {html.escape(sale.remarks)}</p></body></html>"""


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


def write_outputs(sales: Iterable[BankSale], output_dir: str, seller_name: str, seller_gstin: str) -> None:
    out = Path(output_dir)
    receipts_dir = out / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    rows = [dataclasses.asdict(s) for s in sales]
    for idx, row in enumerate(rows, start=1):
        sale = BankSale(**row)
        receipt_no = f"BANK-{idx:05d}"
        row["receipt_no"] = receipt_no
        (receipts_dir / f"{receipt_no}.html").write_text(receipt_html(sale, receipt_no, seller_name, seller_gstin), encoding="utf-8")
    headers = ["receipt_no", *[f.name for f in dataclasses.fields(BankSale)]]
    detail_rows = [[row.get(header, "") for header in headers] for row in rows]
    _write_xlsx(out / "bank_transactions_detailed.xlsx", headers, detail_rows)
    summary_rows = [
        ["transaction_count", len(rows)],
        ["bank_amount", round(sum(row["bank_amount"] for row in rows), 2)],
        ["taxable_value", round(sum(row["taxable_value"] for row in rows), 2)],
        ["cgst", round(sum(row["cgst"] for row in rows), 2)],
        ["sgst", round(sum(row["sgst"] for row in rows), 2)],
    ]
    _write_xlsx(out / "bank_transactions_summary.xlsx", ["metric", "value"], summary_rows)


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
    reader = GoogleSheetReader(config.credentials_file)
    sales = collect_bank_sales(reader, config.drive_path, config.year, config.start_month, config.end_month, config.selling_address)
    write_outputs(sales, config.output_dir, config.seller_name, config.seller_gstin)
    print(json.dumps({"bank_transactions": len(sales), "output_dir": config.output_dir}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
