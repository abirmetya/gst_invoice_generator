from __future__ import annotations

import calendar
import io
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

from gst_invoice_generator.core import RequestConfig, load_request_config
from gst_invoice_generator.service import processed_receipt_days, run_generation

st.set_page_config(page_title="GST Invoice Studio", page_icon="🧾", layout="wide")


def _file_download(path: str | Path, label: str, mime: str) -> None:
    if not path:
        return
    file_path = Path(path)
    if file_path.is_file():
        st.download_button(label, file_path.read_bytes(), file_name=file_path.name, mime=mime, use_container_width=True, on_click="ignore")


def _zip_files(paths: list[str | Path], root: str | Path | None = None) -> bytes | None:
    files = [Path(path) for path in paths if path and Path(path).is_file()]
    if not files:
        return None
    root_path = Path(root) if root else None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            archive_name = file_path.name
            if root_path is not None:
                try:
                    archive_name = str(file_path.relative_to(root_path))
                except ValueError:
                    archive_name = file_path.name
            archive.write(file_path, archive_name)
    return buffer.getvalue()


def _zip_receipts(receipts_dir: str | Path) -> bytes | None:
    if not receipts_dir:
        return None
    directory = Path(receipts_dir)
    if not directory.is_dir():
        return None
    return _zip_files(sorted(directory.rglob("*.pdf")), root=directory)


def _configured_options(raw_config: dict[str, Any], key: str, fallback: str) -> list[str]:
    value = raw_config.get(key, fallback)
    if isinstance(value, list):
        options = [str(item).strip() for item in value if str(item).strip()]
    else:
        options = [str(value).strip()] if str(value).strip() else []
    return options or [fallback]


def _load_sidebar_config(config_file: str) -> tuple[RequestConfig, dict[str, Any]]:
    config_path = Path(config_file)
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    return load_request_config(str(config_path)), raw_config


def _render_receipt_calendar(output_dir: str, default_year: int, default_month: int) -> None:
    days = processed_receipt_days(output_dir)
    st.subheader("Receipt calendar")
    if not days:
        st.info("No dated receipt PDFs found in completed run metadata for this output folder yet.")
        return

    available_dates = sorted(date.fromisoformat(day) for day in days)
    available_years = sorted({item.year for item in available_dates})
    year_index = available_years.index(default_year) if default_year in available_years else len(available_years) - 1
    year_value = st.selectbox("Calendar year", available_years, index=year_index)
    available_months = sorted({item.month for item in available_dates if item.year == year_value})
    month_index = available_months.index(default_month) if default_month in available_months else 0
    month_value = st.selectbox("Calendar month", available_months, index=month_index, format_func=lambda month: f"{month:02d} - {calendar.month_name[month]}")

    st.caption("Days with receipts show a download button. Empty days are intentionally disabled.")
    header_cols = st.columns(7)
    for col, name in zip(header_cols, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], strict=True):
        col.markdown(f"**{name}**")

    for week_index, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(year_value, month_value)):
        cols = st.columns(7)
        for col_index, day_number in enumerate(week):
            with cols[col_index]:
                if day_number == 0:
                    st.write("")
                    continue
                key = f"{year_value:04d}-{month_value:02d}-{day_number:02d}"
                day = days.get(key)
                if not day:
                    st.button(str(day_number), key=f"empty_day_{key}_{week_index}", disabled=True, use_container_width=True)
                    continue
                label = f"{day_number}\n{day['transaction_count']} receipt(s)"
                if receipt_zip := _zip_files(day["receipt_paths"]):
                    st.download_button(
                        label,
                        receipt_zip,
                        file_name=f"receipts-{key}.zip",
                        mime="application/zip",
                        key=f"receipt_day_{key}",
                        use_container_width=True,
                        on_click="ignore",
                    )


def _close_downloads() -> None:
    st.session_state.show_downloads = False


def _render_downloads(metadata: dict[str, Any]) -> None:
    paths = metadata.get("output_paths", {})
    header_cols = st.columns([4, 1])
    with header_cols[0]:
        st.subheader("Download outputs")
    with header_cols[1]:
        st.button("Close", key="close_downloads", use_container_width=True, on_click=_close_downloads)
    cols = st.columns(5)
    with cols[0]:
        _file_download(paths.get("detail_excel", ""), "Detailed Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with cols[1]:
        _file_download(paths.get("department_excel", ""), "Department Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with cols[2]:
        _file_download(paths.get("summary_excel", ""), "Summary Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with cols[3]:
        _file_download(paths.get("metadata", ""), "Metadata JSON", "application/json")
    with cols[4]:
        if receipt_zip := _zip_receipts(paths.get("receipts_dir", "")):
            st.download_button("Receipts ZIP", receipt_zip, file_name="receipts.zip", mime="application/zip", use_container_width=True, on_click="ignore")


st.markdown(
    """
    <style>
    .stApp { background: linear-gradient(135deg, #f8fbff 0%, #f4fff9 100%); }
    .hero { padding: 1.25rem; border-radius: 1.25rem; background: linear-gradient(135deg, #12355b, #1f8a70); color: white; box-shadow: 0 16px 40px rgba(18, 53, 91, .18); }
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
    config_file = st.text_input("Request config JSON", value="request_config.json")
    try:
        base_config, raw_config = _load_sidebar_config(config_file)
    except Exception as exc:
        st.error(f"Could not load request config: {exc}")
        st.stop()

    seller_name = st.selectbox("Seller name", _configured_options(raw_config, "seller_name", base_config.seller_name))
    seller_gstin = st.selectbox("Seller GSTIN", _configured_options(raw_config, "seller_gstin", base_config.seller_gstin))
    selling_address = st.selectbox("Selling address", _configured_options(raw_config, "selling_address", base_config.selling_address))

_render_receipt_calendar(base_config.output_dir, base_config.year, base_config.start_month)

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

if "show_downloads" not in st.session_state:
    st.session_state.show_downloads = False
if "latest_metadata" not in st.session_state:
    st.session_state.latest_metadata = None

if start:
    try:
        config = RequestConfig(
            credentials_file=base_config.credentials_file,
            drive_path=base_config.drive_path,
            year=int(year),
            start_month=int(start_month),
            end_month=int(end_month),
            output_dir=base_config.output_dir,
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
        st.session_state.latest_metadata = metadata
        st.session_state.show_downloads = True
    except Exception as exc:
        st.error(f"Generation failed: {exc}")

if st.session_state.get("show_downloads") and st.session_state.get("latest_metadata"):
    _render_downloads(st.session_state.latest_metadata)
