# GST Invoice Generator

A Python CLI for generating GST-compliant receipts and financial summaries from daily Google Sheets sales records.

The tool is designed for sales data stored in Google Drive under a nested business folder such as:

```text
Google_Business_Data/
└── Daily_Operation/
    ├── 04-April/
    │   ├── Daily_Operations_2026-04-01
    │   ├── Daily_Operations_2026-04-02
    │   └── ...
    ├── 05-May/
    └── 06-June/
```

For a configured year and month range, it reads each Google Sheet's `SALES_ENTRY` tab, finds sales paid through IOB/bank transactions, and creates local receipt/report files.

## What it generates

For every detected bank transaction, the generator writes:

- `receipts/BANK-00001.pdf`, `receipts/BANK-00002.pdf`, etc. — one professionally formatted PDF receipt per bank sale.
- `bank_transactions_detailed.xlsx` — transaction-level details including customer, item, bank amount, taxable value, CGST, SGST, source sheet, and remarks.
- `bank_transactions_summary.xlsx` — the actual bank transaction start/end datetimes plus totals for transaction count, bank amount, taxable value, CGST, and SGST.
- `generation_metadata.json` — run metadata, totals, output paths, and the requested period used to detect and reuse an existing output.

All files are written inside the `output_dir` configured in your request JSON.

## Input sheet requirements

Each daily Google Sheet must contain a tab named `SALES_ENTRY` with the columns below. The tool reads range `A:N`, including the unnamed column immediately after `Remarks`.

```text
Entry_Date | Phone | Customer_Name | Address | Item_Type | Qty_Ordered | Unit | Rate | Order_Value | Paid_Amount | Due_Amount | Order_Ref | Remarks | <unnamed column>
```

## Bank / IOB detection rules

The generator looks for `IOB` markers in both `Remarks` and the unnamed column after `Remarks`.

| Marker | Meaning |
| --- | --- |
| `IOB` | Full `Order_Value` was paid through bank/IOB. |
| `IOB-525` | Only `525` was paid through bank/IOB; the remaining sale value is treated as cash. |
| `IOB:525` | Same as `IOB-525`. |

Only the bank portion is included in generated receipts and reports.

## GST calculation

Bank transaction amounts are treated as **GST-inclusive** at total GST of **5%**:

- CGST: 2.5%
- SGST: 2.5%

For each bank transaction, the tool recalculates:

```text
taxable_value = bank_amount / 1.05
cgst = taxable_value * 0.025
sgst = taxable_value * 0.025
adjusted_rate = taxable_value / Qty_Ordered
```

This means receipts do not blindly reuse the source sheet's `Rate`; the taxable rate is adjusted so the final total equals the bank amount.

## Installation

Create a virtual environment and install the required packages:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Google API setup

1. Create a Google Cloud service account.
2. Enable the Google Drive API and Google Sheets API for the project.
3. Download the service account JSON key file.
4. Share the relevant Drive folder or spreadsheets with the service account email address.
5. Put the JSON key path in your request config as `credentials_file`.

The tool uses read-only Drive and Sheets scopes.

## Request configuration

Run-specific values live in a JSON file. Start by copying the example:

```bash
cp examples/request_config.example.json request_config.json
```

Example config:

```json
{
  "credentials_file": "/secure/service-account.json",
  "drive_path": "Google_Business_Data/Daily_Operation",
  "year": 2026,
  "start_month": 4,
  "end_month": 6,
  "output_dir": "./outputs/apr-jun-2026",
  "seller_name": "Your Business Name",
  "seller_gstin": "YOURGSTIN",
  "selling_address": "Your shop / selling address"
}
```

### Configuration fields

| Field | Required | Description |
| --- | --- | --- |
| `credentials_file` | Yes | Local path to the Google service account JSON key. |
| `drive_path` | Yes | Nested Drive folder that contains month folders, for example `Google_Business_Data/Daily_Operation`. |
| `year` | Yes | Year to process. Sheets with dates in their names are filtered to this year. |
| `start_month` | Yes | First month number to process, from `1` to `12`. |
| `end_month` | Yes | Last month number to process, from `1` to `12`. |
| `output_dir` | Yes | Local directory where receipts and Excel files will be written. |
| `seller_name` | No | Seller/business name printed on receipts. Defaults to `Seller`. |
| `seller_gstin` | No | Seller GSTIN printed on receipts. |
| `selling_address` | No | Seller address printed on receipts. |

Month folders are expected directly under `drive_path` and must use names like `04-April`, `05-May`, and `06-June`.

## Running the generator

Run with your config file:

```bash
python -m gst_invoice_generator --config-file ./request_config.json
```

You can override any config value from the command line for a one-off run. For example, to process only May:

```bash
python -m gst_invoice_generator --config-file ./request_config.json --start-month 5 --end-month 5
```

On success, the command prints a small JSON summary such as:

```json
{
  "status": "created",
  "bank_transactions": 24,
  "output_dir": "outputs/apr-jun-2026",
  "metadata": "outputs/apr-jun-2026/generation_metadata.json"
}
```

## Output layout

If `output_dir` is `./outputs/apr-jun-2026`, the generated files will look like:

```text
outputs/apr-jun-2026/
├── bank_transactions_detailed.xlsx
├── bank_transactions_summary.xlsx
├── generation_metadata.json
└── receipts/
    ├── BANK-00001.pdf
    ├── BANK-00002.pdf
    └── ...
```

## Development and tests

Run the unit test suite with:

```bash
python -m pytest -q
```

Compile-check the package with:

```bash
python -m compileall gst_invoice_generator
```

## Notes and limitations

- Receipts are generated directly as PDF files with ReportLab.
- If `generation_metadata.json` already matches the requested Drive path, year, and month range, the CLI reuses the existing output instead of reading Google Drive and regenerating files.
- Spreadsheet names that include a `YYYY-MM-DD` date are filtered by the configured year/month range.
- Spreadsheet names without a parseable date are processed if they are inside a selected month folder.
- The tool currently reads `SALES_ENTRY!A:N`; add columns after `N` only if the code is updated to read them.
