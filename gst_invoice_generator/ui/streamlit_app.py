from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import streamlit as st

from gst_invoice_generator.core import RequestConfig
from gst_invoice_generator.service import processed_ranges, run_generation

st.set_page_config(page_title="GST Invoice Studio", page_icon="🧾", layout="wide")


def _month_label(month: int | None) -> str:
    if not month:
        return "?"
    return f"{month:02d}"


def _file_download(path: str | Path, label: str, mime: str) -> None:
    file_path = Path(path)
    if file_path.exists():
        st.download_button(label, file_path.read_bytes(), file_name=file_path.name, mime=mime, use_container_width=True)


def _zip_receipts(receipts_dir: str | Path) -> bytes | None:
    directory = Path(receipts_dir)
    if not directory.exists():
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for receipt in sorted(directory.glob("*.pdf")):
            archive.write(receipt, receipt.name)
    return buffer.getvalue()


def _render_processed_ranges(output_dir: str) -> None:
    ranges = processed_ranges(output_dir)
    st.subheader("Already processed ranges")
    if not ranges:
        st.info("No completed run metadata found for this output folder yet.")
        return
    columns = st.columns(min(3, len(ranges)))
    for index, item in enumerate(reversed(ranges[-6:])):
        with columns[index % len(columns)]:
            st.markdown(
                f"""
                <div class="range-card">
                    <div class="range-title">{item.get('year', '?')} · {_month_label(item.get('start_month'))} → {_month_label(item.get('end_month'))}</div>
                    <div>{item.get('transaction_count', 0)} bank transactions</div>
                    <div>₹{float(item.get('bank_amount') or 0):,.2f}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_downloads(metadata: dict[str, Any]) -> None:
    paths = metadata.get("output_paths", {})
    st.subheader("Download outputs")
    cols = st.columns(4)
    with cols[0]:
        _file_download(paths.get("detail_excel", ""), "Detailed Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with cols[1]:
        _file_download(paths.get("summary_excel", ""), "Summary Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with cols[2]:
        _file_download(paths.get("metadata", ""), "Metadata JSON", "application/json")
    with cols[3]:
        if receipt_zip := _zip_receipts(paths.get("receipts_dir", "")):
            st.download_button("Receipts ZIP", receipt_zip, file_name="receipts.zip", mime="application/zip", use_container_width=True)


st.markdown(
    """
    <style>
    .stApp { background: linear-gradient(135deg, #f8fbff 0%, #f4fff9 100%); }
    .hero { padding: 1.25rem; border-radius: 1.25rem; background: linear-gradient(135deg, #12355b, #1f8a70); color: white; box-shadow: 0 16px 40px rgba(18, 53, 91, .18); }
    .range-card { padding: 1rem; border-radius: 1rem; border: 1px solid #dbe8f6; background: rgba(255,255,255,.86); box-shadow: 0 8px 24px rgba(18, 53, 91, .08); }
    .range-title { font-weight: 700; color: #12355b; margin-bottom: .35rem; }
    </style>
    <div class="hero">
      <h1>🧾 GST Invoice Studio</h1>
      <p>Generate bank/IOB GST receipts with a clean control panel, live progress, and one-click downloads.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Run setup")
    credentials_file = st.text_input("Google credentials JSON", value="/secure/service-account.json")
    drive_path = st.text_input("Drive sales folder", value="Google_Business_Data/Daily_Operation")
    output_dir = st.text_input("Output folder", value="./outputs/apr-jun-2026")
    seller_name = st.text_input("Seller name", value="Your Business Name")
    seller_gstin = st.text_input("Seller GSTIN", value="YOURGSTIN")
    selling_address = st.text_area("Selling address", value="Your shop / selling address")

_render_processed_ranges(output_dir)

st.divider()
left, right = st.columns([2, 1])
with left:
    st.subheader("Choose date range")
    range_cols = st.columns(3)
    with range_cols[0]:
        year = st.number_input("Year", min_value=2000, max_value=2100, value=2026, step=1)
    with range_cols[1]:
        start_month = st.selectbox("Start month", range(1, 13), index=3, format_func=lambda m: f"{m:02d}")
    with range_cols[2]:
        end_month = st.selectbox("End month", range(1, 13), index=5, format_func=lambda m: f"{m:02d}")
with right:
    st.subheader("Status")
    st.caption("Notifications appear here while the app works.")

start = st.button("✨ Start generation", type="primary", use_container_width=True)

if start:
    try:
        config = RequestConfig(
            credentials_file=credentials_file,
            drive_path=drive_path,
            year=int(year),
            start_month=int(start_month),
            end_month=int(end_month),
            output_dir=output_dir,
            seller_name=seller_name,
            seller_gstin=seller_gstin,
            selling_address=selling_address,
        )
        config.validate()
        progress = st.progress(0, text="Preparing run…")
        log = st.empty()
        messages: list[str] = []

        def notify(message: str) -> None:
            messages.append(message)
            progress.progress(min(95, 10 + len(messages) * 7), text=message)
            log.markdown(f"**Latest:** {message}")
            st.toast(message, icon="🟢")

        with st.status("Generating invoices…", expanded=True) as status:
            metadata = run_generation(config, progress_callback=notify)
            for message in messages[-10:]:
                st.write(message)
            status.update(label="Done", state="complete")
        progress.progress(100, text="Ready for download")
        st.success("Generation completed successfully." if metadata.get("status") == "created" else "Existing output reused successfully.")
        totals = metadata.get("totals", {})
        c1, c2, c3 = st.columns(3)
        c1.metric("Transactions", totals.get("transaction_count", 0))
        c2.metric("Bank amount", f"₹{float(totals.get('bank_amount') or 0):,.2f}")
        c3.metric("Status", metadata.get("status", "done"))
        _render_downloads(metadata)
    except Exception as exc:
        st.error(f"Generation failed: {exc}")
