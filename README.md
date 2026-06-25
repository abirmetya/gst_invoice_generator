# GST Invoice Generator

Generate GST-compliant bank-sale receipts from Google Sheets stored under a nested Google Drive base path like `Google_Business_Data/Daily_Operation`, with month folders like:

```text
drive_path/mm-Month/Daily_Operations_YYYY-mm-dd
```

The tool walks month folders such as `04-April` directly under the configured Drive path, then reads each spreadsheet's `SALES_ENTRY` tab (`A:N`), detects bank transactions marked as `IOB` or `IOB-amount` in `Remarks` or the unnamed column immediately after `Remarks`, and writes:

- one HTML sale receipt per bank transaction;
- `bank_transactions_summary.xlsx` with totals; and
- `bank_transactions_detailed.xlsx` with transaction-level records.

Bank transaction amounts are treated as GST-inclusive at 5% total GST (2.5% CGST + 2.5% SGST). The receipt taxable rate is recalculated from the bank amount instead of blindly using the sheet rate.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Google credentials

Create a Google Cloud service account with Drive and Sheets read access, download its JSON key, and share the relevant Drive folders/spreadsheets with the service account email.

## Run

```bash
python -m gst_invoice_generator \
  --credentials-file /secure/service-account.json \
  --drive-path "Google_Business_Data/Daily_Operation" \
  --year 2026 \
  --start-month 4 \
  --end-month 6 \
  --output-dir ./outputs/apr-jun-2026 \
  --seller-name "Your Business Name" \
  --seller-gstin "YOURGSTIN" \
  --selling-address "Your shop / selling address"
```

## IOB parsing rules

- `IOB`: the full `Order_Value` is a bank transaction.
- `IOB-525` or `IOB:525`: only that amount is a bank transaction; the balance is assumed to be cash.
- The same patterns are recognized in the unnamed column after `Remarks`.
