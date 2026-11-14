# Francis - Advanced Data Preprocessing and Collation
# Copyright (C) 2026 Jan Derrfuss
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. This program is distributed WITHOUT ANY WARRANTY; without even
# the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/>. Source: https://github.com/jderrfuss/francis

"""Francis — a single-file Streamlit app for preprocessing and collating
PsychoPy CSV output into one analysis-ready spreadsheet.

Execution model: like every Streamlit script, this file runs top to bottom on
every user interaction (a "rerun"). There is no callback graph — widget values
persist in st.session_state across reruns. The browser caches preprocessing
until its data or settings change. Comments throughout flag the places where this model
needs care (e.g. re-asserting multiselect state, caching the parsed upload).

Pipeline order (top to bottom in this file):
  1. Helper / engine functions and the expression-safety allowlist.
  2. Sidebar: upload + global filters (minimum rows, loop filter).
  3. Ordered preprocessing blocks (new columns, missing-data and extreme-value
     removal) applied to the filtered frame.
  4. Analysis tabs (Reaction Times, Error Rates) that build per-condition
     configs from the preprocessed frame.
  5. Download logic that freezes the results and settings together in a ZIP.

User-supplied filter / recode / formula expressions are evaluated by pandas, so
every one is vetted by _check_expr_safety (an AST allowlist plus a constant-fold
size guard) before it reaches df.query / df.eval.
"""

# stlite: browser prototype. This is a copy of francis_app/app.py (commit
# e738175) adapted to run in the browser via stlite (Pyodide). Every change from
# the server version is marked with a "# stlite:" comment. See README.md here.

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import zipfile
import io
import re
import ast
import math
import os
import sys
import uuid
import datetime
import json
# stlite: sqlite3 removed with the visit counter (see below).
import hashlib
import time  # stlite: timings for the diagnostics panel
import asyncio  # stlite: cooperative progress / cancellation
import gc  # stlite: release obsolete datasets before another upload
import copy  # stlite: preserve setup while a replacement decision is pending
from analysis_engine import analyze_vectorised_steps  # stlite: fast analysis

# stlite: shallow frames isolate writes without eagerly copying every column.
pd.options.mode.copy_on_write = True
from collections import Counter

_T_RUN0 = time.perf_counter()  # stlite: start of this run, for the diagnostics panel
_DATA_ALIASES = ('_df_raw', '_pipeline', '_pp_snap', '_snap', 'df_main_process',
                 'df_row_filtered', 'df_rt_res', 'df_err_res', 'final_df', 'res_df')  # stlite


def wasm_memory_mb():
    """stlite: current size of Pyodide's WebAssembly heap in MB, or None.

    WebAssembly memory only ever grows, so this is the high-water mark of the
    tab's Python memory use. Shown in the diagnostics panel for testing.
    """
    try:
        import pyodide_js
        return pyodide_js._module.HEAPU8.length / (1024 * 1024)
    except Exception:
        return None

# ==========================================
# 1. HELPER FUNCTIONS
# ==========================================

# stlite: the SQLite visit counter (DB_PATH / track_visit) is removed. In the
# browser the database would live in each visitor's own tab memory, so it could
# never count anything. Page loads can be counted from the web server's log.

# stlite: Paco's yield-before-Streamlit checkpoint, generalised to any unit.
def finish_steps(steps):
    """Run the same calculation synchronously in native/Pyodide harnesses."""
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


async def report_progress(progress, done, total, text):
    for _ in range(3):
        await asyncio.sleep(0)
    # Every checkpoint calls Streamlit to service Cancel. Timed redraws avoid
    # Paco's fixed 50 ms pause for every ten fast conditions.
    progress.progress(min(done / max(total, 1), 1.0),
                      text=f"{text}: {done:,} of {total:,}")
    now = time.perf_counter()
    if now - st.session_state.get('_progress_redraw_at', 0) >= 0.1:
        st.session_state._progress_redraw_at = now
        await asyncio.sleep(0.01)


async def await_steps(steps, progress):
    try:
        while True:
            try:
                done, total, text = next(steps)
            except StopIteration as result:
                # Service a final Cancel/rerun before publishing the result,
                # without replacing useful file/row counts with "1 of 1".
                for _ in range(3):
                    await asyncio.sleep(0)
                if isinstance(progress, DelayedBusyStatus):
                    progress.progress(1., text='Finished')  # internal; never displayed
                else:
                    progress.empty()
                return result.value
            await report_progress(progress, done, total, text)
    finally:
        steps.close()  # cancelled generators release their partial frames/buffers


def progress_with_cancel(slot, key, on_cancel):
    with slot.container():
        # stlite: stack controls so narrow sidebars never split "Cancel".
        bar = st.empty()
        st.button("Cancel", key=key, on_click=on_cancel)
    return bar


# stlite: internal analysis phases use different denominators. Show one stable
# status instead of pretending that their local counts form an overall percent.
class DelayedBusyStatus:
    def __init__(self, slot, key, on_cancel, message, delay=0.35):
        self.slot = slot
        self.key = key
        self.on_cancel = on_cancel
        self.message = message
        self.delay = delay
        self.started = time.perf_counter()
        self.status = None

    def progress(self, value, *, text):
        if self.status is None:
            # Do not flash a working indicator for a result already completed.
            if text == 'Finished' or time.perf_counter() - self.started < self.delay:
                self.slot.empty()  # still a Streamlit cancellation/rerun checkpoint
                return
            with self.slot.container():
                self.status = st.empty()
                st.button('Cancel', key=self.key, on_click=self.on_cancel)
        # Repeating identical text services Cancel without changing the visual
        # status as conditions, participant statistics and CSV stages alternate.
        self.status.info(self.message, icon='⏳')


async def run_with_progress(steps, key, on_cancel, container=None, busy_message=None):
    slot = (container if container is not None else st).empty()
    progress = (DelayedBusyStatus(slot, key, on_cancel, busy_message)
                if busy_message else progress_with_cancel(slot, key, on_cancel))
    try:
        return await await_steps(steps, progress)
    finally:
        slot.empty()


def release_dataset():
    for key in list(st.session_state):
        if key in ('_df_raw', '_name_col', '_load_msgs', '_pipeline_cache',
                   '_raw_file_counts', '_export_snapshot', '_preproc_snapshot',
                   '_last_parsed_sig', '_current_upload_transition',
                   '_applied_config_file_id') or key.startswith(('_pp_', '_formula_err_')):
            st.session_state.pop(key, None)
    # stlite: reruns may retain names in the script namespace as well as state.
    # Drop aliases so an old processed frame/export cannot survive a new parse.
    for key in _DATA_ALIASES:
        globals().pop(key, None)
    gc.collect()


def cancel_upload():
    st.session_state._data_uploader_key = st.session_state.get('_data_uploader_key', 0) + 1
    transition = st.session_state.pop('_current_upload_transition', None)
    if transition and transition['settings'] is not None:
        st.session_state._pending_dataset_change = transition
    release_dataset()
    st.session_state._operation_notice = "Reading the upload was cancelled."


def cancel_analysis():
    st.session_state._operation_notice = "Analysis was cancelled; no new results were saved."


def cancel_preprocessed():
    st.session_state.pop('_preproc_snapshot', None)
    st.session_state._operation_notice = "Preparing preprocessed data was cancelled."
    gc.collect()


def write_csv_steps(frame, buffer, batch_rows):
    """stlite: UTF-8 bytes directly, one header, original row/column order."""
    frame.iloc[:0].to_csv(buffer, index=False, encoding='utf-8')
    for start in range(0, len(frame), batch_rows):
        yield start, len(frame), "Preparing CSV rows"
        frame.iloc[start:start + batch_rows].to_csv(
            buffer, index=False, header=False, encoding='utf-8')


def csv_steps(frame, batch_rows=5000):
    with io.BytesIO() as buffer:
        yield from write_csv_steps(frame, buffer, batch_rows)
        return buffer.getvalue()


def analysis_zip_steps(frame, report, configuration, folder, generated_at):
    def entry(filename):
        info = zipfile.ZipInfo(f'{folder}/{filename}', generated_at.timetuple()[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info._compresslevel = 1  # ZipInfo's public compress_level needs Python 3.13.
        return info

    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, 'w') as archive:
            # Write batches directly into the archive to avoid buffering a second copy of the CSV.
            with archive.open(entry('results.csv'), 'w', force_zip64=True) as csv_file:
                yield from write_csv_steps(frame, csv_file, 5000)
            archive.writestr(entry('settings_report.md'), report)
            archive.writestr(entry('run_configuration.json'), configuration)
        return buffer.getvalue()


def load_data(uploaded_file):
    # stlite: synchronous adapter for regression tests; UI awaits these steps.
    return finish_steps(load_data_steps(uploaded_file))


# These patterns only decide whether a diagnostic read is worthwhile. Compare
# the actual parsed cells so quoted commas, multiline fields and blanks are
# interpreted by the same CSV parser as the analysis data.
_LEADING_ZERO_INTEGER = r'[ \t]*[+-]?0[0-9]+[ \t]*'
# Nonempty default NA tokens in the pinned pandas runtime.
_CSV_INFERENCE_TOKEN = re.compile(
    rb'[,\r\n]"?(?:(' + _LEADING_ZERO_INTEGER.encode() + rb')'
    rb'|#N/A(?: N/A)?|#NA|-?1\.#(?:IND|QNAN)|-?NaN|-?nan|<NA>|N/A|NA|NULL|None|n/a|null)'
    rb'"?(?=[,\r\n]|\Z)'
)


def csv_inference_hints(source):
    padded = set()
    has_missing = False
    with source.getbuffer() as raw:
        for match in _CSV_INFERENCE_TOKEN.finditer(raw):
            start, stop = match.span(1)
            if start == -1:
                has_missing = True
            elif padded is not None:
                if stop - start <= 64:
                    padded.add(match[1].decode('ascii'))
                if stop - start > 64 or len(padded) > 64:
                    # Bound the hint table. Check spelling directly for this file.
                    padded = None
            if has_missing and padded is None:
                break
    return padded, has_missing


def could_have_padded_integers(values):
    kind = values.dtype.kind
    if kind == 'f':
        numeric = values.to_numpy()
        return bool(np.any(np.isfinite(numeric) & (numeric == np.trunc(numeric))))
    return kind in 'iu' or (kind == 'O' and pd.api.types.infer_dtype(values) in ('mixed', 'mixed-integer'))


def csv_inference_examples(source, frame):
    """Find examples of labels altered by the normal read without changing it."""
    if frame.empty:
        return {}
    padded_tokens, check_missing = csv_inference_hints(source)
    zero_cols = ([col for col in frame if could_have_padded_integers(frame[col])]
                 if padded_tokens or padded_tokens is None else [])
    missing_cols = [col for col in frame if frame[col].hasnans] if check_missing else []
    columns = list(dict.fromkeys(zero_cols + missing_cols))
    if not columns:
        return {}

    # usecols changes field alignment when pandas infers an implicit index.
    usecols = columns if isinstance(frame.index, pd.RangeIndex) else None
    width = len(columns) if usecols is not None else len(frame.columns)
    source.seek(0)
    changes = {}
    offset = 0
    with pd.read_csv(source, dtype=str, keep_default_na=False, usecols=usecols,
                     chunksize=max(1, 100_000 // width)) as reader:
        for original in reader:
            stop = offset + len(original)
            for col in missing_cols:
                key = (col, 'missing')
                if key not in changes:
                    lost = frame[col].iloc[offset:stop].isna().to_numpy()
                    tokens = original[col][lost & original[col].notna() & original[col].ne('')]
                    if not tokens.empty:
                        changes[key] = repr(tokens.iloc[0])
            for col in zero_cols:
                key = (col, 'leading zero')
                if key not in changes:
                    if padded_tokens is None:
                        padded = original[col].str.fullmatch(_LEADING_ZERO_INTEGER, na=False).to_numpy()
                    else:
                        padded = original[col].isin(padded_tokens).to_numpy()
                    if not padded.any():
                        continue
                    tokens = original[col].to_numpy()[padded]
                    values = frame[col].iloc[offset:stop].to_numpy()[padded]
                    for token, value in zip(tokens, values):
                        if not isinstance(value, str):
                            example = token if len(token) <= 64 else token[:64] + '…'
                            changes[key] = f'{example!r} became {value}'
                            break
            if len(changes) == len(zero_cols) + len(missing_cols):
                break
            offset = stop
    return changes


def read_csv_with_diagnostics(source, path, diagnostics, warn):
    frame = pd.read_csv(source)
    try:
        changes = csv_inference_examples(source, frame)
    except (TypeError, ValueError, IndexError):
        warn(f"Could not check {path!r} for leading-zero values or text interpreted as missing.")
    else:
        for key, example in changes.items():
            entry = diagnostics.setdefault(key, {'count': 0, 'examples': []})
            entry['count'] += 1
            if len(entry['examples']) < 3:
                entry['examples'].append(f'{example} in {path!r}')
    return frame


def mixed_csv_type_warnings(frame, file_spans):
    """Describe text mixed with non-text CSV values using the final column names."""
    infer_dtype = pd.api.types.infer_dtype
    for column in frame.select_dtypes(include='object'):
        values = frame[column]
        # Ignore numeric dtype differences and columns containing only text and blanks.
        if infer_dtype(values) not in ('mixed', 'mixed-integer'):
            continue
        if not any(isinstance(value, str) for value in values):
            continue

        files_by_type = {}
        for path, start, stop in file_spans:
            kind = infer_dtype(values.iloc[start:stop])
            if kind != 'empty':
                files_by_type.setdefault(kind, []).append(path)

        details = []
        for kind, paths in files_by_type.items():
            label = {'string': 'text', 'mixed': 'mixed types',
                     'mixed-integer': 'mixed types'}.get(kind, kind)
            examples = ', '.join(repr(path) for path in paths[:3])
            if len(paths) > 3:
                examples += f' and {len(paths) - 3:,} more'
            details.append(f'{label} in {examples}')
        yield (
            f"Column {column!r} mixes text with other value types ({', '.join(details)}). "
            "Conditions can split and filters can omit trials. "
            "Check this column in the source CSVs before interpreting results."
        )


_CSV_INFERENCE_LABELS = {
    'leading_zero': 'Leading zeros removed',
    'text_missing': 'Text read as missing',
}


def load_data_steps(uploaded_file):
    """Handles Zip or CSV uploads, merges them, and adds a filename bookkeeping
    column, with zip-bomb / OOM protection.

    Returns (full_df, name_col, messages). On failure full_df is None.
    `name_col` is normally 'file_name' but falls back to 'file_name_francis'
    (then _1, _2, ...) if the uploaded data already contains a 'file_name'
    column, so the user's own column is never overwritten. Because the whole
    pipeline (grouping, row filter, shifts) keys on this column, it is threaded
    downstream rather than hard-coded.

    `messages` is a list of (kind, text) tuples for upload warnings, errors
    and CSV inference notices. They are collected rather than emitted because the
    parse result is cached by content hash: an st.warning emitted here would
    appear only on the single rerun that parses the upload and vanish on the
    next interaction. The call site caches the list alongside the frame and
    re-displays it on every rerun while the upload is active.
    """
    all_dfs = []                 # list of (source_path, df)
    msgs = []
    inference_diagnostics = {}
    def _warn(text): msgs.append(('warning', text))
    def _error(text): msgs.append(('error', text))

    # --- SECURITY LIMITS ---
    MAX_FILE_MB = 50
    MAX_TOTAL_MB = 200           # uncompressed zip-bomb / OOM guard
    MAX_OUTPUT_MB = 1000         # cap on in-RAM frame footprint after concat
    MAX_SINGLE_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
    MAX_TOTAL_BYTES = MAX_TOTAL_MB * 1024 * 1024
    progress_total, progress_text = 1, "Reading CSV"  # stlite: one continuous bar

    # Streamlit reuses the upload buffer across reruns; rewind defensively so a
    # prior read can never leave us positioned mid-stream.
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    if uploaded_file.name.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(uploaded_file) as z:
                total_uncompressed_size = 0

                csv_members = [n for n in z.namelist()
                               if n.lower().endswith('.csv') and not n.startswith('__MACOSX')]
                # stlite: name-based ZIP lookup would read the last repeated
                # entry twice. Reject ambiguous CSV paths before reading any.
                repeated_paths = sorted(n for n, count in Counter(csv_members).items() if count > 1)
                if repeated_paths:
                    listed = ', '.join(repr(n) for n in repeated_paths[:8])
                    extra = f" (and {len(repeated_paths) - 8} more)" if len(repeated_paths) > 8 else ''
                    _error(f"Upload rejected: repeated CSV paths in the ZIP: {listed}{extra}. "
                           "Each CSV must have a unique path.")
                    return None, 'file_name', msgs
                progress_total, progress_text = len(csv_members), "Reading files"
                for file_i, filename in enumerate(csv_members):
                    yield file_i, progress_total, progress_text  # stlite
                    if filename.lower().endswith('.csv') and not filename.startswith('__MACOSX'):

                        # 1. Fast metadata check (header)
                        info = z.getinfo(filename)
                        if info.file_size > MAX_SINGLE_FILE_BYTES:
                            _warn(f"Skipped '{filename}': Header claims it exceeds {MAX_FILE_MB}MB.")
                            continue

                        # 2. Robust stream check. Read the member defensively: an
                        # encrypted entry raises RuntimeError and a bad-CRC entry
                        # raises BadZipFile mid-read. Handle both per file so a
                        # single bad member is skipped rather than crashing the
                        # page or aborting the whole upload (the outer handler
                        # only covers a ZIP broken at the archive level).
                        try:
                            with z.open(filename) as f:
                                raw_bytes = f.read(MAX_SINGLE_FILE_BYTES + 1)
                        except Exception:
                            _warn(f"Skipped '{filename}': could not be read (encrypted or corrupt).")
                            continue

                        if len(raw_bytes) > MAX_SINGLE_FILE_BYTES:
                            _warn(f"Skipped '{filename}': Actual size exceeds {MAX_FILE_MB}MB during decompression.")
                            continue

                        total_uncompressed_size += len(raw_bytes)
                        if total_uncompressed_size > MAX_TOTAL_BYTES:
                            # Refuse outright rather than return a silently truncated dataset.
                            _error(
                                f"Upload rejected: total uncompressed data exceeds the "
                                f"{MAX_TOTAL_MB}MB limit. Please upload a smaller ZIP."
                            )
                            return None, 'file_name', msgs

                        try:
                            df = read_csv_with_diagnostics(io.BytesIO(raw_bytes), filename, inference_diagnostics, _warn)
                            all_dfs.append((filename, df))
                        except Exception as error:
                            # stlite: keep the readable files, with a cached,
                            # filename-specific warning on every later rerun.
                            if isinstance(error, pd.errors.EmptyDataError):
                                reason = "empty CSV (no columns found)"
                            elif isinstance(error, UnicodeError):
                                reason = "unsupported text encoding"
                            elif isinstance(error, pd.errors.ParserError):
                                reason = "invalid CSV format"
                            else:
                                reason = "CSV could not be parsed"
                            _warn(f"Skipped '{filename}': {reason}.")
                            continue
        except zipfile.BadZipFile:
            _error("The uploaded file is not a valid ZIP archive.")
            return None, 'file_name', msgs

    elif uploaded_file.name.lower().endswith('.csv'):
        # Streamlit already limits the initial upload size, so native CSVs are safe
        try:
            yield 0, progress_total, progress_text  # stlite: one parse remains atomic
            df = read_csv_with_diagnostics(uploaded_file, uploaded_file.name, inference_diagnostics, _warn)
        except Exception:
            _error("The uploaded CSV file could not be read.")
            return None, 'file_name', msgs
        all_dfs.append((uploaded_file.name, df))

    if not all_dfs:
        # Report the empty-result case explicitly; otherwise the only feedback
        # would be the generic "Could not read files" message in the sidebar.
        _error("No readable CSV files were found in the upload.")
        return None, 'file_name', msgs

    # Resolve each file's identifier. Use the bare basename when it is unique;
    # for basenames that occur in more than one file (e.g. 's1/data.csv' and
    # 's2/data.csv') use the complete ZIP path so they stay distinct even when
    # different participants have identically named nested session folders.
    # This preserves file identities for grouping, row-count filters and shifts.
    # Build the per-row labels in concat order so the bookkeeping
    # column can be inserted after we know the final (normalised) column set.
    basenames = [os.path.basename(p) for p, _ in all_dfs]
    collided = {b for b, n in Counter(basenames).items() if n > 1}
    name_values = []
    file_spans = []
    for (path, df), base in zip(all_dfs, basenames):
        if df.empty:
            _warn(f"'{path}' has a header but no data rows and contributes no trials.")
        if base in collided:
            label = path
        else:
            label = base
        file_spans.append((path, len(name_values), len(name_values) + len(df)))
        name_values.extend([label] * len(df))

    # Primary guard (pre-concat): heterogeneous column sets make concat union
    # into a wide, NaN-filled frame that can vastly exceed the sum of its
    # inputs — and the allocation happens DURING pd.concat, so the post-concat
    # memory check below would never get the chance to run. Estimate the
    # concatenated footprint from the already-parsed frames (rows x
    # union-of-raw-columns x 8 bytes, the float64/NaN floor) and refuse
    # before allocating it.
    total_rows = sum(len(df) for _, df in all_dfs)
    union_cols = set()
    for _, df in all_dfs:
        union_cols.update(df.columns)
    est_mb = total_rows * len(union_cols) * 8 / (1024 * 1024)
    if est_mb > MAX_OUTPUT_MB:
        _error(
            f"Upload rejected: collating these files would need at least "
            f"~{est_mb:,.0f}MB of memory (limit {MAX_OUTPUT_MB}MB), likely "
            f"because many CSVs have non-overlapping columns."
        )
        return None, 'file_name', msgs

    # stlite: include object/string cells and missing columns in the existing
    # 1000 MB guard before allocating concat. Keep existing upload limits.
    estimated_bytes = sum(int(df.memory_usage(deep=True).sum())
                          + len(df) * (len(union_cols) - len(df.columns)) * 8
                          for _, df in all_dfs)
    estimated_bytes += sum(len(str(label).encode('utf-8')) + 64 for label in name_values)
    if estimated_bytes > MAX_OUTPUT_MB * 1024 * 1024:
        _error(f"Upload rejected: estimated collated data exceeds the {MAX_OUTPUT_MB}MB memory limit.")
        return None, 'file_name', msgs
    # stlite: service Cancel before concat without resetting a completed bar.
    # Collation is brief for typical studies, so it needs no separate UI phase.
    yield progress_total, progress_total, progress_text
    full_df = pd.concat([df for _, df in all_dfs], ignore_index=True)
    all_dfs.clear()  # stlite: release source frames promptly after concat
    source_columns = list(full_df.columns)
    full_df.columns = full_df.columns.str.strip().str.replace(r'\W+', '_', regex=True)

    # Normalising punctuation to '_' can map two distinct source columns onto the
    # same name (e.g. 'key_resp.rt' and 'key_resp_rt' both become 'key_resp_rt').
    # Duplicate labels make df[col] return a DataFrame and break selection
    # downstream, so rename the later occurrence(s) with a numeric suffix and tell
    # the user which names clashed.
    cols = list(full_df.columns)
    if len(set(cols)) < len(cols):
        original = set(cols)
        used, renamed, new_cols = set(), [], []
        for c in cols:
            if c not in used:
                used.add(c)
                new_cols.append(c)
            else:
                i = 2
                cand = f"{c}_{i}"
                while cand in original or cand in used:
                    i += 1
                    cand = f"{c}_{i}"
                used.add(cand)
                new_cols.append(cand)
                renamed.append(c)
        full_df.columns = new_cols
        _warn(
            "After normalising column names, some clashed and were renamed with a "
            f"numeric suffix so they stay distinct: {', '.join(sorted(set(renamed)))}. "
            "This usually means a file mixes dot-style and underscore-style names "
            "(e.g. 'key_resp.rt' and 'key_resp_rt')."
        )

    # Pick the name for the bookkeeping column. Normally 'file_name', but if the
    # data already has its own 'file_name' column we must not clobber it, so fall
    # back to 'file_name_francis' (then _1, _2, ...). Checked against the
    # normalised columns so a user column like 'file name' is also detected.
    name_col = 'file_name'
    if name_col in full_df.columns:
        base_name, suffix = 'file_name_francis', 0
        name_col = base_name
        while name_col in full_df.columns:
            suffix += 1
            name_col = f"{base_name}_{suffix}"
        _warn(
            "Your files already contain a 'file_name' column, so Francis added "
            f"its own filename column as '{name_col}' to avoid overwriting your data."
        )

    normalized_columns = dict(zip(source_columns, full_df.columns))
    full_df.insert(0, name_col, name_values)

    if collided:
        _warn(
            f"{len(collided)} filename(s) occurred in more than one folder. "
            f"Those files use their full ZIP paths in '{name_col}' so they remain distinct."
        )

    # Secondary guard (post-concat): the pre-concat estimate above uses the
    # 8-bytes-per-cell float64 floor, so a frame heavy in Python-object
    # (string) cells can still come out larger than estimated. Re-check the
    # real footprint before handing the frame back.
    output_mb = full_df.memory_usage(deep=True).sum() / (1024 * 1024)
    if output_mb > MAX_OUTPUT_MB:
        _error(
            f"Upload rejected: the collated dataset would use ~{output_mb:.0f}MB "
            f"of memory (limit {MAX_OUTPUT_MB}MB), likely due to many CSVs with "
            f"non-overlapping columns."
        )
        return None, 'file_name', msgs

    for warning in mixed_csv_type_warnings(full_df.drop(columns=name_col), file_spans):
        _warn(warning)

    for (column, kind), entry in inference_diagnostics.items():
        examples = ', '.join(entry['examples'])
        if extra := entry['count'] - len(entry['examples']):
            examples += f' and {extra:,} more file(s)'
        column = normalized_columns[column]
        if kind == 'leading zero':
            msgs.append(('leading_zero', f"Column {column!r} lost leading zeros during CSV parsing ({examples}). "
                         "Distinct IDs or condition labels can merge."))
        else:
            msgs.append(('text_missing', f"Column {column!r} contains text read as missing during CSV parsing ({examples}). "
                         "If these values are IDs or condition labels, they can be omitted from the analysis."))

    return full_df, name_col, msgs

def format_val_for_name(x):
    """Helper to format numbers nicely (1.0 -> '1')."""
    try:
        f = float(x)
        if f.is_integer(): return str(int(f))
        return str(x)
    except (ValueError, TypeError):
        return str(x)

def clean_col_name(col):
    """Removes non-alphanumeric chars (except underscore) for safety."""
    return re.sub(r'\W+', '', col)

def format_col_val_label(col, val):
    clean_col = clean_col_name(col)
    val_str = format_val_for_name(val)
    if val_str and val_str[0].isalpha():
        return f"{clean_col}{val_str.capitalize()}"
    else:
        return f"{clean_col}{val_str}"

def sort_combs(combs, by_cols):
    """Sort condition combinations for the preview/config builders.

    Collation easily produces a condition column that mixes types (file A has
    `block` as numbers, file B has a stray text value in the same column), and
    the natural sort then raises TypeError. Fall back to a string-keyed sort:
    clean numeric columns keep their numeric ordering, mixed columns sort
    lexicographically instead of failing the whole block."""
    try:
        return combs.sort_values(by=by_cols)
    except TypeError:
        return combs.sort_values(by=by_cols, key=lambda s: s.astype(str))

def fmt_query_value(val):
    """Render a data value as a literal for a generated pandas-query string.

    Strings go through repr() so quotes and backslashes in the data (e.g. a
    condition value of don't) are escaped correctly; naive '{val}' quoting
    would produce a SyntaxError for them. str() first because a numpy str_
    subclass would repr as "np.str_('x')" instead of a plain quoted literal.
    Non-strings (numbers, booleans) keep their plain str() rendering."""
    return repr(str(val)) if isinstance(val, str) else str(val)

# Cap on the number of conditions a single analysis block may generate. Each
# distinct combination of the grouping columns' values becomes one condition,
# and analyze_data_blocks runs one df.query PER condition PER participant — so an
# unbounded count (the classic foot-gun: grouping on a continuous reaction-time
# column instead of a categorical factor) turns into a per-request CPU/memory
# blow-up that freezes the single-process app. The expression constant-fold guard
# does not cover this — the generated queries are individually valid. Blocks that
# breach the cap are skipped with a clear message rather than executed.
MAX_CONDITIONS_PER_BLOCK = 500

class _TooManyConditions(Exception):
    """Raised when a block's grouping columns yield more than
    MAX_CONDITIONS_PER_BLOCK conditions (almost always a continuous column picked
    by mistake). Carries the offending count in args[0]."""

# --- OUTLIER CALCULATION FUNCTIONS ---

def calculate_mad_bounds(series, threshold):
    median = series.median()
    abs_deviations = (series - median).abs()
    mad = 1.4826 * abs_deviations.median()
    if mad == 0: return (-np.inf, np.inf)
    lower = median - (threshold * mad)
    upper = median + (threshold * mad)
    return lower, upper

def calculate_double_mad_bounds(series, threshold):
    """Calculates asymmetric bounds using Double MAD. Permissive if MAD is 0."""
    median = series.median()
    left_split = series[series <= median]
    right_split = series[series >= median]
    
    mad_left = 1.4826 * (left_split - median).abs().median()
    mad_right = 1.4826 * (right_split - median).abs().median()
    
    if mad_left == 0: lower = -np.inf
    else: lower = median - (threshold * mad_left)

    if mad_right == 0: upper = np.inf
    else: upper = median + (threshold * mad_right)
        
    return lower, upper

def calculate_sd_bounds(series, threshold):
    mean = series.mean()
    sd = series.std()
    if sd == 0: return (-np.inf, np.inf)
    lower = mean - (threshold * sd)
    upper = mean + (threshold * sd)
    return lower, upper

def calculate_trimmed_bounds(series, percent):
    lower = series.quantile(percent / 100.0)
    upper = series.quantile(1 - (percent / 100.0))
    return lower, upper

def parse_values_string(val_str):
    if not val_str.strip(): return []
    parts = [p.strip() for p in val_str.split(',')]
    clean_parts = []
    for p in parts:
        try:
            f = float(p)
            if f.is_integer(): clean_parts.append(int(f))
            else: clean_parts.append(f)
        except ValueError:
            clean_parts.append(p.strip("'").strip('"'))
    return clean_parts

def derive_rt_col(corr_col):
    """Derive the paired RT column name from a PsychoPy accuracy column name.
    Handles both underscore-style (key_resp_corr → key_resp_rt) and
    dot-style (key_resp.corr → key_resp.rt) naming conventions for response components."""
    if corr_col and corr_col.endswith('_corr'):
        return corr_col[:-5] + '_rt'
    if corr_col and corr_col.endswith('.corr'):
        return corr_col[:-5] + '.rt'
    return ''

# Expression nodes permitted in user-supplied filter / recode / formula text.
# This is an ALLOWLIST: only column names, literals, boolean / comparison /
# arithmetic operators and list / tuple literals (for `x in [...]`) are accepted.
# Filters and Recode conditions allow no calls. Custom Formula can opt in to
# the specific direct maths calls below. Attribute access remains outside the
# allowlist so expressions cannot invoke arbitrary methods on columns.
_ALLOWED_EXPR_NODES = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
)
_FORMULA_MATH_FUNCTIONS = {
    'sqrt': math.sqrt, 'abs': abs, 'log': math.log, 'log10': math.log10, 'exp': math.exp,
    'floor': math.floor, 'ceil': math.ceil, 'log1p': math.log1p, 'expm1': math.expm1,
}

# Backtick-quoted column names (e.g. `2back`) are a pandas extension, not valid
# Python, so they must be removed before ast.parse can run. Their contents are
# only ever looked up as a column label — never evaluated as code — so swapping
# each one for a placeholder identifier is safe and preserves the surrounding
# structure for validation.
_BACKTICK_RE = re.compile(r'`[^`]*`')

# The arithmetic operators allowed above can still exhaust CPU / memory
# when both operands are constants: pandas folds a pure-constant subexpression in
# Python before handing it to numexpr, so `9**9**9**9` builds a 370-million-digit
# integer (CPU/GIL burn) and `'a' * 10**9` allocates a gigabyte string. The AST
# walk cannot see this — `**` and `*` are allowed nodes. So after the node check
# we additionally fold the constant parts of the expression under a size ceiling
# and reject any breach. Column-involving arithmetic is intentionally NOT bounded:
# it runs in numpy/numexpr with fixed-width types and overflows rather than growing
# without limit, so `col**2`, `col*col`, `2*col` and `rt*1000000000` (ns scaling)
# stay valid. String repetition is rejected outright — `'a'*col` would repeat a
# column element-wise on the engine='python' Recode path, which folding can't see.
_MAX_CONST_DIGITS = 100       # ceiling on a folded integer's decimal length (pre-pow estimate)
_MAX_CONST_STRLEN = 100_000   # ceiling on a folded string's length
_MAX_RESULT_BITS = 512        # backstop on any folded integer (~154 digits)

class _ExprTooHeavy(Exception):
    """Raised when folding a constant subexpression would exceed a size cap."""

_VAR_TERM = object()  # marks a subtree that references a column — not a pure constant

_CONST_BINOPS = {
    ast.Add: lambda a, b: a + b,   ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,  ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
    ast.BitAnd: lambda a, b: a & b, ast.BitOr: lambda a, b: a | b,
    ast.BitXor: lambda a, b: a ^ b,
}

def _pow_too_big(base, exp):
    """True if base**exp would exceed the digit ceiling, decided WITHOUT computing
    it. Only int**int with a non-negative exponent grows a Python big integer; a
    float operand overflows to inf in O(1) and a negative exponent yields a float."""
    if isinstance(base, bool) or isinstance(exp, bool):
        return False
    if isinstance(base, int) and isinstance(exp, int) and exp >= 0 and abs(base) >= 2:
        # len(str(abs(base))) over-estimates digit growth per unit exponent, so the
        # bound is conservative; base is already folded-and-bounded, so str() is cheap.
        return exp * len(str(abs(base))) > _MAX_CONST_DIGITS
    return False

def _fold_const(node):
    """Fold the constant parts of an allow-listed expression under a size cap.

    Returns the folded Python value for a constant subtree, or `_VAR_TERM` for any
    subtree that references a column. Raises `_ExprTooHeavy` if a `Pow`/`Mult` (or
    a folded literal) would breach a ceiling, or on string repetition. Both
    operands of every BinOp are folded BEFORE the column short-circuit, so a
    dangerous constant subexpression is caught even next to a column (e.g.
    `col + 9**9**9`)."""
    if isinstance(node, ast.Expression):
        return _fold_const(node.body)
    if isinstance(node, ast.Constant):
        # str AND bytes: a long bytes literal is just as expensive to hold, and
        # bytes is not a str, so it would otherwise slip past this cap.
        if isinstance(node.value, (str, bytes)) and len(node.value) > _MAX_CONST_STRLEN:
            raise _ExprTooHeavy
        return node.value
    if isinstance(node, ast.Name):
        return _VAR_TERM
    if isinstance(node, ast.Call):
        # Calls reach the fold only after validation of the name and arguments.
        # Fold their arguments and preserve constant integer results from abs()
        # so they cannot bypass the checks on a surrounding operation.
        argument = _fold_const(node.args[0])
        if argument is _VAR_TERM:
            return _VAR_TERM
        try:
            return _FORMULA_MATH_FUNCTIONS[node.func.id](argument)
        except (TypeError, ValueError, OverflowError):
            return _VAR_TERM
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            _fold_const(elt)
        return _VAR_TERM
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _fold_const(v)
        return _VAR_TERM
    if isinstance(node, ast.Compare):
        _fold_const(node.left)
        for c in node.comparators:
            _fold_const(c)
        return _VAR_TERM
    if isinstance(node, ast.UnaryOp):
        val = _fold_const(node.operand)
        if val is _VAR_TERM:
            return _VAR_TERM
        try:
            if isinstance(node.op, ast.USub):   return -val
            if isinstance(node.op, ast.UAdd):   return +val
            if isinstance(node.op, ast.Invert): return ~val
            if isinstance(node.op, ast.Not):    return not val
        except Exception:
            return _VAR_TERM
        return _VAR_TERM
    if isinstance(node, ast.BinOp):
        left = _fold_const(node.left)
        right = _fold_const(node.right)
        # Reject str/bytes repetition outright: 'a'*1e9, b'a'*1e9, 'a'*col,
        # ('a'+'b')*col. bytes must be included — it is not a str, so without it
        # `b'a' * 10**9` folds to a gigabyte buffer right here inside the check.
        if isinstance(node.op, ast.Mult) and (isinstance(left, (str, bytes)) or isinstance(right, (str, bytes))):
            raise _ExprTooHeavy
        # A column operand → numpy/numexpr, fixed width, cannot explode.
        if left is _VAR_TERM or right is _VAR_TERM:
            return _VAR_TERM
        if isinstance(node.op, ast.Pow) and _pow_too_big(left, right):
            raise _ExprTooHeavy
        fn = _CONST_BINOPS.get(type(node.op))
        if fn is None:
            return _VAR_TERM
        try:
            result = fn(left, right)
        except _ExprTooHeavy:
            raise
        except Exception:
            # ZeroDivisionError, TypeError, etc. — a runtime error, not a safety
            # problem; let pandas raise it at evaluation time.
            return _VAR_TERM
        if isinstance(result, bool):
            return result
        if isinstance(result, int) and result.bit_length() > _MAX_RESULT_BITS:
            raise _ExprTooHeavy
        if isinstance(result, (str, bytes)) and len(result) > _MAX_CONST_STRLEN:
            raise _ExprTooHeavy
        return result
    # Any other allow-listed node: treat as non-constant.
    return _VAR_TERM

def _check_expr_safety(expr, *, allow_math=False):
    """Return True only if `expr` is built solely from allow-listed nodes AND its
    constant subexpressions stay within the folding size caps.

    Vets every user-supplied expression before it reaches df.query / df.eval.
    Attribute access and arbitrary calls are rejected. The constant-fold pass
    also blocks the arithmetic
    resource-exhaustion that the operator allowlist alone permits (`9**9**9**9`,
    `'a'*10**9`). Empty expressions are treated as safe (no-op). Anything that
    fails to parse is rejected: pandas uses the same Python grammar, so it would
    have raised SyntaxError anyway (a too-long integer literal raises ValueError,
    also rejected here)."""
    if not expr or not expr.strip():
        return True
    cleaned = _BACKTICK_RE.sub('_fg_col_', expr)
    try:
        tree = ast.parse(cleaned, mode='eval')
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and allow_math:
            if (not isinstance(node.func, ast.Name) or node.func.id not in _FORMULA_MATH_FUNCTIONS
                    or len(node.args) != 1 or node.keywords):
                return False
        elif not isinstance(node, _ALLOWED_EXPR_NODES):
            return False
    try:
        _fold_const(tree)
    except (_ExprTooHeavy, RecursionError):
        return False
    return True

def safe_query(df, expr):
    """Query with scope isolation (blocks @variable injection) and user-friendly error reporting."""
    if not _check_expr_safety(expr):
        st.warning(
            f"Filter expression was blocked: `{expr}`. Only column names, numbers, "
            "comparisons, arithmetic and `in [...]` are allowed — method calls "
            "(e.g. `.str.contains`) and attribute access are not permitted."
        )
        raise ValueError(f"Unsafe expression blocked: {expr}")
    try:
        return df.query(expr, local_dict={}, global_dict={})
    except pd.errors.UndefinedVariableError:
        st.warning(f"Filter references an undefined variable — remove any `@` references: `{expr}`")
        raise
    except (ValueError, SyntaxError) as e:
        st.warning(f"Invalid filter syntax: `{expr}` — {e}")
        raise

def organize_final_columns(df, meta_cols):
    cols = list(df.columns)
    present_meta = [c for c in dict.fromkeys(meta_cols) if c in cols]
    metadata = set(present_meta)
    
    glob_list = ["trls_total", "trls_missing", "trls_extreme_global", "trls_valid_global"]
    present_globs = [c for c in glob_list if c in cols and c not in metadata]
    
    # --- SMARTER CHECK ---
    # Only classify as a detail column if the base condition actually exists in the dataframe
    def is_count(c):
        if '_trls_' in c: return True
        for suff in ['_denom', '_num', '_lower', '_upper']:
            if c.endswith(suff):
                prefix = c[:-len(suff)]
                if prefix in cols:
                    return True
        return False
    # ---------------------
    
    detail_counts = [c for c in cols if c not in metadata and c not in present_globs and is_count(c)]
    
    reserved = set(present_meta + present_globs + detail_counts)
    values = [c for c in cols if c not in reserved]
    
    # Sort detail columns logically
    def get_count_sort_key(col_name):
        suffix_map = {
            "_trls_pre_filter": 0, "_trls_rejected_by_filter": 1, "_trls_post_filter": 2,
            "_trls_excluded_dv": 3,
            "_lower": 4, "_upper": 5, "_trls_outlier": 6, "_trls_final": 7,
            "_num": 8, "_denom": 9
        }
        best_suffix = ""
        rank = 99
        for suff, r in suffix_map.items():
            if col_name.endswith(suff):
                if len(suff) > len(best_suffix):
                    best_suffix = suff
                    rank = r
        stem = col_name[:-len(best_suffix)] if best_suffix else col_name
        return (stem, rank)

    detail_counts.sort(key=get_count_sort_key)
    return df[present_meta + present_globs + values + detail_counts]

def report_runtime_versions():
    versions = {}
    # The browser entrypoint mounts the build metadata from its source URL version.
    try:
        with open(os.path.join(os.path.dirname(__file__), 'francis-build.json'), encoding='utf-8') as source:
            versions.update(json.load(source))
    except (OSError, ValueError):
        pass  # Native runs and older entrypoints may lack this file.
    versions.update({'Python': sys.version.split()[0], 'pandas': pd.__version__, 'NumPy': np.__version__})
    if streamlit_version := getattr(st, '__version__', None):
        versions['Streamlit'] = streamlit_version
    if sys.platform == 'emscripten':
        import pyodide
        versions['Pyodide'] = pyodide.__version__
    return versions


def formula_infinity_message(positive, negative):
    return f"Formula produced infinite values ({positive:,} positive, {negative:,} negative)."


def generate_settings_report(settings, rt_configs, err_configs, group_method, part_col, preprocessing_blocks):
    """Generates a Markdown string mirroring the app structure."""
    lines = []
    lines.append(f"# Francis Analysis Log")
    lines.append(f"**Generated:** {settings['timestamp']}")
    lines.append("")

    lines.append("## 1. Input Data")
    if upload := settings.get('upload_signature'):
        filename, size, digest = upload
        lines.append(f"- **Upload** — {filename!r} ({size:,} bytes)")
        lines.append(f"- **Upload SHA-256** — `{digest}`")
    if row_counts := settings.get('row_counts'):
        loaded, preprocessed, results = row_counts
        lines.append(f"- **Rows** — {loaded:,} loaded, {preprocessed:,} after preprocessing, {results:,} in results.")
    lines.append(f"- **Participant Column:** {part_col}")
    lines.append(f"- **Grouping Method:** {group_method}")
    lines.append(f"- **Metadata Columns Kept:** {', '.join(settings.get('keep_cols', [])) if settings.get('keep_cols') else '(None)'}")
    if retention := settings.get('data_retention'):
        for kind, count in retention['loaded'].items():
            label = kind[0].upper() + kind[1:]
            lines.append(f"- **{label} with data** — {count:,} at load, "
                         f"{retention['remaining'][kind]:,} after preprocessing.")
        for stage, excluded in retention['excluded'].items():
            lines.extend(f"- {message}" for message in exclusion_messages(stage, excluded))
    for level, message in settings.get('load_messages', []):
        if level == 'warning':
            lines.append(f"- Upload warning — {message}")
        elif level in _CSV_INFERENCE_LABELS:
            lines.append(f"- CSV parsing note — {message}")
    if versions := settings.get('runtime_versions'):
        lines.append(f"- **Build and runtime** — {', '.join(f'{name} {version}' for name, version in versions.items())}.")
    lines.append("")

    lines.append("## 2. Global Filters (Sidebar)")
    lines.append("### Minimum Row Number Filter")
    lines.append(f"- **Enabled:** {settings['use_min_rows']}")
    if settings['use_min_rows']:
        lines.append(f"- **Min Rows:** {settings['min_row_count']}")
    lines.append("### Main Loop(s) Filter")
    lines.append(f"- **Loops Selected:** {', '.join(settings['loop_cols']) if settings['loop_cols'] else '(None)'}")
    lines.append("")

    _next_sec = 3
    lines.append(f"## {_next_sec}. Data Preprocessing (in order)")
    if preprocessing_blocks:
        for i, b in enumerate(preprocessing_blocks, 1):
            btype = b.get('type', 'New Column — Arithmetic')
            if btype == 'New Column — Shift Values':
                grouping = "source file"
                if participant_column := b.get('participant_column'):
                    grouping += f" and participant ID column `{participant_column}`"
                lines.append(f"{i}. **New Column — Shift Values — {b['new_col_name']}**: "
                             f"Shift `{b['col_a']}` by {b['shift_amt']} (grouped by {grouping})")
                if missing_ids := b.get('missing_participant_ids'):
                    lines.append(f"   - Shifted values left blank on {missing_ids:,} trial(s) with missing participant IDs.")
            elif btype == 'New Column — Arithmetic':
                t = b
                op_name = t['operation'].split(" (")[0]
                if t.get('col_b'):
                    lines.append(f"{i}. **New Column — Arithmetic — {t['new_col_name']}**: {op_name} (`{t['col_a']}`, `{t['col_b']}`)")
                else:
                    lines.append(f"{i}. **New Column — Arithmetic — {t['new_col_name']}**: {op_name} (`{t['col_a']}`)")
            elif btype == 'New Column — Recode Values':
                lines.append(f"{i}. **New Column — Recode Values — {b['new_col_name']}**")
                if b.get('output_type') == 'Number':
                    lines.append("   - Output type — Number (blank values are missing)")
                for cond, val in b.get('rules', []):
                    lines.append(f"   - If `{cond}` → `{val}`")
                lines.append(f"   - *(default)* → `{b.get('default', '')}`")
            elif btype == 'New Column — Custom Formula':
                lines.append(f"{i}. **New Column — Custom Formula — {b['new_col_name']}**: `{b['formula']}`")
                if counts := b.get('infinity_counts'):
                    lines.append(f"   - {formula_infinity_message(*counts)}")
            elif btype == 'Missing Data Removal':
                cols     = ', '.join(b.get('cols', [])) or '(None)'
                criteria = ', '.join(b.get('criteria', ['NaN'])) or 'NaN'
                lines.append(f"{i}. **Missing Data Removal** — Columns: {cols} · Remove if: {criteria}")
            elif btype == 'Extreme Value Rejection':
                lines.append(f"{i}. **Extreme Value Rejection** — Column: `{b.get('col', '')}`, Range: {b.get('min_v', 0):g} to {b.get('max_v', 0):g}")
    else:
        lines.append("- (No preprocessing blocks configured)")
    lines.append("")
    _next_sec += 1

    lines.append(f"## {_next_sec}. Analysis Output List")
    lines.append("### Trial Counts & Limits")
    lines.append(f"- **Include Counts:** {settings['show_counts']}")
    if settings['show_counts']:
        lines.append(f"  - *Note: Lower and Upper columns in the output represent the calculated outlier cut-off boundaries.*")
    lines.append("")

    def _group_by_block(configs):
        """Return (seen_block_nums, {block_num: [configs]}) preserving insertion order."""
        seen, groups = [], {}
        for cfg in configs:
            bn = cfg.get('block_num', 1)
            if bn not in groups:
                groups[bn] = []
                seen.append(bn)
            groups[bn].append(cfg)
        return seen, groups

    def _append_conditions(gcols, block_cfgs):
        """Append **Conditions** tree to lines (mutates lines via closure)."""
        if not gcols:
            lines.append("**Conditions:** (None — all trials combined)")
        else:
            lines.append("**Conditions**")
            col_values = {}
            for cfg in block_cfgs:
                for col in gcols:
                    val = cfg.get('row_values', {}).get(col)
                    if val is not None:
                        col_values.setdefault(col, [])
                        if val not in col_values[col]:
                            col_values[col].append(val)
            for col, vals in col_values.items():
                lines.append(f"- `{col}`")
                for v in vals:
                    lines.append(f"  - `{v}`")

    def _append_output_cols(block_cfgs):
        if len(block_cfgs) == 1:
            lines.append(f"**Output column:** `{block_cfgs[0]['Condition_Name']}`")
        else:
            lines.append("**Output columns:**")
            for cfg in block_cfgs:
                lines.append(f"- `{cfg['Condition_Name']}`")
        lines.append("")

    if rt_configs:
        lines.append("### Reaction Time Outputs")
        seen_blocks, block_groups = _group_by_block(rt_configs)
        for bn in seen_blocks:
            block_cfgs = block_groups[bn]
            first = block_cfgs[0]
            lines.append(f"#### Block {bn}")
            # 1. Conditions
            _append_conditions(first.get('group_cols', []), block_cfgs)
            lines.append("")
            # 2. Dependent Variable
            lines.append(f"**Dependent Variable:** `{first['Dependent_Var']}`")
            lines.append("")
            # 3. Trial Exclusion
            lines.append("**Trial Exclusion**")
            filt = first.get('Extra_Logic', '').strip()
            lines.append(f"- Trial Filter: `{filt}`" if filt else "- Trial Filter: (None)")
            if first.get('enable_outliers'):
                method = first['outlier_method']
                thresh = first['outlier_thresh']
                scope = first['outlier_scope']
                thresh_str = f"{thresh}%" if method == "Percentage Trimming" else str(thresh)
                lines.append(f"- Outlier Rejection: {method} ({thresh_str}), {scope}")
                if scope.startswith("Global") and filt:
                    lines.append(f"  - *Global bounds restricted to trials matching trial filter*")
            else:
                lines.append("- Outlier Rejection: Disabled")
            suffix = first.get('suffix', '').strip()
            lines.append(f"- Condition name suffix: `{suffix}`" if suffix else "- Condition name suffix: (None)")
            lines.append("")
            # 4. Summary Measure
            ms_str = " (converted to ms)" if first.get('convert_to_ms') else ""
            lines.append(f"**Summary Measure:** {first['Measure']}{ms_str}")
            if first['Measure'] in ("Mean", "Median"):
                lines.append(f"- Trials with DV = 0 excluded: {'Yes' if first.get('exclude_zero_dv', True) else 'No'}")
            lines.append("")
            # 5. Min Trials
            lines.append(f"**Minimum Trials per Condition:** {first.get('min_trials', 1)}")
            lines.append("")
            _append_output_cols(block_cfgs)

    if err_configs:
        lines.append("### Error Rate Outputs")
        seen_blocks_e, block_groups_e = _group_by_block(err_configs)
        for bn in seen_blocks_e:
            block_cfgs = block_groups_e[bn]
            first = block_cfgs[0]
            lines.append(f"#### Block {bn}")
            # 1. Conditions
            _append_conditions(first.get('group_cols', []), block_cfgs)
            lines.append("")
            # 2. Target Column
            lines.append(f"**Target Column:** `{first['Target_Column']}`")
            lines.append("")
            # 3. Define Ratio
            lines.append("**Define Ratio**")
            lines.append(f"- Numerator values: {first.get('Numerator_Values', '').strip()}")
            lines.append(f"- Denominator values: {first.get('Denominator_Values', '').strip()}")
            rt_col = first.get('rt_col_for_timeouts', '')
            no_response_status = f"Yes (via `{rt_col}`)" if rt_col else 'No'
            if first.get('no_response_requested') and not rt_col:
                if expected := first.get('expected_rt_col'):
                    no_response_status += f". Exclusion was requested. Paired RT column `{expected}` was unavailable."
                else:
                    no_response_status += ". Exclusion was requested. The target column has no paired RT name."
            lines.append(f"- Exclude trials with no response: {no_response_status}")
            lines.append("")
            # 4. Trial Filter
            filt_e = first.get('Extra_Logic', '').strip()
            lines.append(f"**Trial Filter:** `{filt_e}`" if filt_e else "**Trial Filter:** (None)")
            lines.append("")
            # 5. Output
            lines.append("**Output**")
            suffix_e = first.get('suffix', '').strip()
            lines.append(f"- Condition name suffix: `{suffix_e}`" if suffix_e else "- Condition name suffix: (None)")
            lines.append(f"- Format: {'Percentage' if first.get('Scale_To_Pct') else 'Ratio (0–1)'}")
            lines.append(f"- Minimum Trials per Condition (denominator): {first.get('min_trials', 1)}")
            lines.append("")
            _append_output_cols(block_cfgs)

    return "\n".join(lines)

# --- TRANSFORMATION ENGINE ---
def arithmetic_column(frame, operation, col_a, col_b):
    """Compute the output before assigning it to the frame."""
    a = frame[col_a]
    if operation == "Concatenate (A_B)":
        b = frame[col_b]
        # Missing inputs stay missing instead of becoming the text "nan".
        return (a.map(format_val_for_name) + "_" + b.map(format_val_for_name)).mask(
            a.isna() | b.isna()
        )

    a = pd.to_numeric(a, errors='coerce')
    if operation == "Log (Natural: ln A)":
        return np.log(a.replace(0, np.nan))
    if operation == "Log (Base 10: log10 A)":
        return np.log10(a.replace(0, np.nan))
    if operation == "Square Root (sqrt A)":
        return np.sqrt(a)
    if operation == "Inverse (1 / A)":
        return 1 / a.replace(0, np.nan)
    if operation == "Absolute Value (|A|)":
        return np.abs(a)

    b = pd.to_numeric(frame[col_b], errors='coerce')
    if operation == "Ratio (A / B)":
        return a / b.replace(0, np.nan)
    if operation == "Multiplication (A * B)":
        return a * b
    if operation == "Sum (A + B)":
        return a + b
    if operation == "Difference (A - B)":
        return a - b
    raise ValueError(f"Unknown arithmetic operator {operation!r}.")


def parse_recode_number(value, label):
    text = str(value).strip()
    if not text:
        return np.nan
    try:
        number = pd.to_numeric(text)
    except (ValueError, OverflowError):
        raise ValueError(f"{label} must be a number or blank.") from None
    if not np.isfinite(number):
        raise ValueError(f"{label} must be a finite number or blank.")
    return number


def apply_conditional_column(df, cfg, warn=None):  # stlite
    """Evaluate ordered rules and assign output values to a new column.
    Rules are (condition_str, output_value) pairs; first match wins.
    Rows matching no rule receive the default value."""
    warn = warn or st.warning  # stlite: cache/replay preprocessing messages
    new_col  = cfg.get('new_col_name', '').strip()
    rules    = cfg.get('rules', [])
    default  = cfg.get('default', '')

    if not new_col:
        return df

    dtype = object
    if cfg.get('output_type') == 'Number':
        default = parse_recode_number(default, 'The default value')
        rules = [(condition, parse_recode_number(value, f"The output for `{condition}`"))
                 for condition, value in rules]
        values = [default, *(value for _, value in rules)]
        dtype = pd.Series(values).dtype
        if dtype.kind not in 'iuf' or (
            dtype.kind == 'f' and any(isinstance(value, np.integer) and int(float(value)) != int(value)
                                     for value in values)
        ):
            raise ValueError("The configured numbers cannot share a column without losing integer precision.")
    else:
        default = str(default)
        rules = [(condition, str(value)) for condition, value in rules]

    df_out = df.copy(deep=False)  # stlite: copy-on-write
    result = pd.Series(default, index=df_out.index, dtype=dtype)

    # Apply in reverse so the first rule in the list has highest priority
    for cond_str, out_val in reversed(list(rules)):
        cond_str = cond_str.strip()
        if not cond_str:
            continue
        if not _check_expr_safety(cond_str):
            warn(f"⚠️ Conditional column '{new_col}': rule `{cond_str}` was blocked (unsafe expression).")
            continue
        try:
            matched_idx = df_out.query(cond_str, local_dict={}, global_dict={}, engine='python').index
            result.loc[matched_idx] = out_val
        except Exception as e:
            warn(f"⚠️ Conditional column '{new_col}': rule `{cond_str}` failed and was skipped: {e}")

    df_out[new_col] = result
    return df_out

# ==========================================
# 2. ANALYSIS ENGINE (REUSABLE)
# ==========================================
def analyze_legacy_steps(df, blocks, group_col, keep_cols, settings, mode="RT", name_col='file_name'):  # stlite: original semantics
    if not blocks: return pd.DataFrame()

    results = []
    final_configs = []
    exec_name_tracker = {}
    
    # Store skipped items as strings for detailed reporting
    skipped_items = []
    
    for row in blocks:
        row = dict(row)  # copy so we don't mutate the caller's list
        base = row['Condition_Name']
        if base in exec_name_tracker:
            exec_name_tracker[base] += 1
            row['Condition_Name'] = f"{base}_{exec_name_tracker[base]}"
        else:
            exec_name_tracker[base] = 1
        final_configs.append(row)
    
    config_df = pd.DataFrame(final_configs)
    grouped = df.groupby(group_col)
    
    for sub_i, (sub_id, sub_df) in enumerate(grouped):
        yield sub_i, grouped.ngroups, "Analysing participants (compatibility)"  # stlite
        row_res = {group_col: sub_id}
        for k in keep_cols:
            if k in sub_df.columns and not sub_df[k].empty:
                if k == name_col:
                    # When merging files per participant, list all source files rather
                    # than silently picking an arbitrary one.
                    unique_files = sub_df[k].dropna().unique()
                    row_res[k] = ' | '.join(str(f) for f in unique_files)
                else:
                    row_res[k] = sub_df[k].iloc[0]

        valid_df = sub_df.copy(deep=False)  # stlite
        
        # --- PRE-COMPUTED COUNTS FROM PREPROCESSING PIPELINE ---
        # Missing data and extreme value filtering are done in the preprocessing
        # pipeline before this function is called. Counts are stored in session state.
        if settings['show_counts']:
            row_res["trls_missing"] = st.session_state.get('_pp_missing_removed', {}).get(sub_id, 0)
            row_res["trls_extreme_global"] = st.session_state.get('_pp_extreme_removed', {}).get(sub_id, 0)

        # --- MODE SPECIFIC PROCESSING ---
        if mode == "RT":
            if settings['show_counts']:
                # trls_total: rows per participant before preprocessing blocks ran
                row_res["trls_total"] = st.session_state.get('_pp_total_counts', {}).get(sub_id, len(sub_df))
                row_res["trls_valid_global"] = len(valid_df)

        for _, row in config_df.iterrows():
            c_name = row['Condition_Name']
            if mode == "RT":
                c_dv = row['Dependent_Var']
                c_meas = row['Measure']
                
                do_outlier = row.get('enable_outliers', False)
                o_scope = row.get('outlier_scope', 'Per Condition')
                o_method = row.get('outlier_method', 'Standard Deviation (SD)')
                o_thresh = row.get('outlier_thresh', 2.5)
                o_base_logic = row.get('Extra_Logic', '')

                try:
                    group_cols = row.get('group_cols')
                    ungrouped = isinstance(group_cols, list) and not group_cols
                    trial_filter = row['Filter_Logic']
                    selected = valid_df if ungrouped and not trial_filter else safe_query(valid_df, trial_filter)
                    cond_data = selected.copy(deep=False)  # stlite
                    n_post_filter = len(cond_data)
                    if settings['show_counts']:
                        if ungrouped or row.get('Factor_Logic'):
                            try:
                                n_pre = len(valid_df) if ungrouped else len(safe_query(valid_df, row['Factor_Logic']))
                                row_res[f"{c_name}_trls_pre_filter"] = n_pre
                                row_res[f"{c_name}_trls_rejected_by_filter"] = n_pre - n_post_filter
                            except Exception: pass
                        row_res[f"{c_name}_trls_post_filter"] = n_post_filter

                    cond_data[c_dv] = pd.to_numeric(cond_data[c_dv], errors='coerce')
                    # NaN DVs (no response / non-numeric) are never valid
                    # observations, for any measure: Sum skips them anyway,
                    # Count is documented as "valid trials", and leaving them
                    # in let the outlier mask drop them and miscount them as
                    # outliers for Sum/Count. Zeros are excluded only when the
                    # block asks for it, and only for Mean/Median (raw RTs:
                    # 0 is a recording artifact; difference / log DVs: 0 is
                    # real data). Negative values are always kept — log RTs
                    # and difference scores are legitimately negative.
                    cond_data = cond_data[cond_data[c_dv].notna()]
                    if c_meas in ["Mean", "Median"] and row.get('exclude_zero_dv', True):
                        cond_data = cond_data[cond_data[c_dv] != 0]
                    n_before = len(cond_data)
                    # Trials dropped between post_filter and here (missing DV
                    # always; zero DV for Mean/Median when enabled) had no count
                    # column, so post_filter - outlier - final did not reconcile.
                    # Surface the difference: post_filter - excluded_dv - outlier
                    # - final == 0.
                    if settings['show_counts']:
                        row_res[f"{c_name}_trls_excluded_dv"] = n_post_filter - n_before
                    
                    # --- OUTLIER REJECTION ---
                    L_disp, U_disp = np.nan, np.nan
                    
                    if do_outlier:
                        if o_scope.startswith("Global"):
                            base_df = valid_df.copy(deep=False)  # stlite
                            if o_base_logic.strip():
                                try: base_df = safe_query(valid_df, o_base_logic)
                                except Exception: pass
                            
                            v_base = pd.to_numeric(base_df[c_dv], errors='coerce')
                            # Mirror the condition-level exclusion so global bounds are
                            # computed from the same trial population they are applied to.
                            v_valid_base = v_base.dropna()
                            if c_meas in ["Mean", "Median"] and row.get('exclude_zero_dv', True):
                                v_valid_base = v_valid_base[v_valid_base != 0]
                            
                            if len(v_valid_base) >= 4:
                                if o_method == "Standard Deviation (SD)": L, U = calculate_sd_bounds(v_valid_base, o_thresh)
                                elif o_method == "Median Absolute Deviation (MAD)": L, U = calculate_mad_bounds(v_valid_base, o_thresh)
                                elif o_method == "Double MAD": L, U = calculate_double_mad_bounds(v_valid_base, o_thresh)
                                else: L, U = calculate_trimmed_bounds(v_valid_base, o_thresh)
                                
                                L_disp, U_disp = L, U
                                mask = (cond_data[c_dv] >= L) & (cond_data[c_dv] <= U)
                                cond_data = cond_data[mask]
                                if settings['show_counts']: row_res[f"{c_name}_trls_outlier"] = n_before - len(cond_data)
                            else:
                                skipped_items.append(f"{sub_id}: {c_name} (Global)")

                        elif o_scope.startswith("Per Condition"):
                            if n_before >= 4:
                                if o_method == "Standard Deviation (SD)": L, U = calculate_sd_bounds(cond_data[c_dv], o_thresh)
                                elif o_method == "Median Absolute Deviation (MAD)": L, U = calculate_mad_bounds(cond_data[c_dv], o_thresh)
                                elif o_method == "Double MAD": L, U = calculate_double_mad_bounds(cond_data[c_dv], o_thresh)
                                else: L, U = calculate_trimmed_bounds(cond_data[c_dv], o_thresh)
                                
                                L_disp, U_disp = L, U
                                mask = (cond_data[c_dv] >= L) & (cond_data[c_dv] <= U)
                                cond_data = cond_data[mask]
                                if settings['show_counts']: row_res[f"{c_name}_trls_outlier"] = n_before - len(cond_data)
                            else:
                                skipped_items.append(f"{sub_id}: {c_name}")
                        
                        if settings['show_counts']:
                            if row.get('convert_to_ms', False):
                                if not np.isinf(L_disp) and not np.isnan(L_disp): L_disp = round(L_disp * 1000, 2)
                                if not np.isinf(U_disp) and not np.isnan(U_disp): U_disp = round(U_disp * 1000, 2)
                            else:
                                if not np.isinf(L_disp) and not np.isnan(L_disp): L_disp = round(L_disp, 3)
                                if not np.isinf(U_disp) and not np.isnan(U_disp): U_disp = round(U_disp, 3)
                            if np.isinf(L_disp): L_disp = np.nan
                            if np.isinf(U_disp): U_disp = np.nan
                            row_res[f"{c_name}_lower"] = L_disp
                            row_res[f"{c_name}_upper"] = U_disp
                            
                    n_final = len(cond_data)
                    if settings['show_counts']: row_res[f"{c_name}_trls_final"] = n_final
                    block_min = row.get('min_trials', 1)
                    if n_final < block_min:
                        val = np.nan
                    else:
                        if c_meas == "Mean": val = cond_data[c_dv].mean()
                        elif c_meas == "Median": val = cond_data[c_dv].median()
                        elif c_meas == "Sum": val = cond_data[c_dv].sum()
                        else: val = n_final
                        
                        if row.get('convert_to_ms', False) and c_meas in ["Mean", "Median"]:
                            val *= 1000
                            val = round(val)
                    row_res[c_name] = val
                except (pd.errors.UndefinedVariableError, ValueError, SyntaxError):
                    row_res[c_name] = np.nan  # Warning already shown by safe_query
                except Exception as e:
                    row_res[c_name] = np.nan
                    st.warning(f"⚠️ Unexpected error computing '{c_name}' for '{sub_id}': {type(e).__name__}: {e}")

            elif mode == "Error":
                try:
                    # Optionally exclude time-out trials (rows where the global RT column is NaN)
                    err_base_df = valid_df
                    rt_col_for_to = row.get('rt_col_for_timeouts', '')
                    if rt_col_for_to and rt_col_for_to in err_base_df.columns:
                        rt_vals = pd.to_numeric(err_base_df[rt_col_for_to], errors='coerce')
                        err_base_df = err_base_df[rt_vals.notna()]

                    denom_df = safe_query(err_base_df, row['Filter_Logic_Denominator'])
                    err_min = row.get('min_trials', 1)
                    if len(denom_df) < err_min:
                        row_res[c_name] = np.nan
                        if settings['show_counts']:
                            row_res[f"{c_name}_denom"] = len(denom_df)
                            row_res[f"{c_name}_num"] = np.nan
                    else:
                        num_df = safe_query(denom_df, row['Numerator_Logic'])
                        ratio = len(num_df)/len(denom_df)
                        if row.get('Scale_To_Pct', True):
                            row_res[c_name] = round(ratio * 100, 2)
                        else:
                            row_res[c_name] = round(ratio, 5)
                        if settings['show_counts']:
                            row_res[f"{c_name}_denom"] = len(denom_df)
                            row_res[f"{c_name}_num"] = len(num_df)
                except (pd.errors.UndefinedVariableError, ValueError, SyntaxError):
                    row_res[c_name] = np.nan  # Warning already shown by safe_query
                except Exception as e:
                    row_res[c_name] = np.nan
                    st.warning(f"⚠️ Unexpected error computing '{c_name}' for '{sub_id}': {type(e).__name__}: {e}")

        results.append(row_res)
        
    if skipped_items:
        st.warning(f"⚠️ Outlier rejection skipped for {len(skipped_items)} condition(s) due to insufficient data (N < 4).")
        with st.expander("See affected conditions (Participant : Condition)"):
            st.dataframe(pd.DataFrame(skipped_items, columns=["Skipped Conditions"]), hide_index=True)
        
    return pd.DataFrame(results)

# stlite: restart the entire RT/Error call on any speculative error. This
# preserves original name disambiguation, partial counts and per-person warnings.
def analyze_data_blocks_steps(df, blocks, group_col, keep_cols, settings,
                             mode="RT", name_col='file_name'):
    st.session_state.pop(f"_trial_funnel_{mode}", None)  # set by the whole-frame path only
    try:
        result = yield from analyze_vectorised_steps(
            globals(), df, blocks, group_col, keep_cols, settings, mode, name_col)
        st.session_state._diag_analysis_path = 'whole-frame'
        return result
    except Exception:
        st.session_state._diag_analysis_path = 'compatibility fallback'
        return (yield from analyze_legacy_steps(
            df, blocks, group_col, keep_cols, settings, mode, name_col))


def analyze_data_blocks(df, blocks, group_col, keep_cols, settings,
                        mode="RT", name_col='file_name'):
    return finish_steps(analyze_data_blocks_steps(
        df, blocks, group_col, keep_cols, settings, mode, name_col))


# ==========================================
# 3. SESSION STATE MANAGEMENT
# ==========================================
_NEW_COLUMN_TYPES = ("New Column — Arithmetic", "New Column — Recode Values",
                     "New Column — Shift Values", "New Column — Custom Formula")


def output_column_name(block_type, value):
    if block_type not in _NEW_COLUMN_TYPES or not isinstance(value, str):
        return ''
    # Older Arithmetic configurations can refer to the exact padded name.
    # The Arithmetic widget trims fresh edits before the pipeline runs.
    if block_type == "New Column — Arithmetic" and value.strip():
        return value
    return value.strip()


def trim_arithmetic_name(key):
    st.session_state[key] = st.session_state[key].strip()


if 'rt_blocks' not in st.session_state: st.session_state.rt_blocks = [str(uuid.uuid4())] 
if 'err_blocks' not in st.session_state: st.session_state.err_blocks = [str(uuid.uuid4())]
if 'trans_blocks' not in st.session_state: st.session_state.trans_blocks = [str(uuid.uuid4())]

def add_rt_block(): st.session_state.rt_blocks.append(str(uuid.uuid4()))
def remove_rt_block(b): 
    if b in st.session_state.rt_blocks: st.session_state.rt_blocks.remove(b)

def add_err_block(): st.session_state.err_blocks.append(str(uuid.uuid4()))
def remove_err_block(b): 
    if b in st.session_state.err_blocks: st.session_state.err_blocks.remove(b)

def add_trans_block(): st.session_state.trans_blocks.append(str(uuid.uuid4()))

def move_trans_block_up(b):
    blocks = st.session_state.trans_blocks
    i = blocks.index(b)
    if i > 0:
        blocks[i], blocks[i - 1] = blocks[i - 1], blocks[i]

def move_trans_block_down(b):
    blocks = st.session_state.trans_blocks
    i = blocks.index(b)
    if i < len(blocks) - 1:
        blocks[i], blocks[i + 1] = blocks[i + 1], blocks[i]
def remove_trans_block(b):
    if b in st.session_state.trans_blocks: st.session_state.trans_blocks.remove(b)
    # Clean up any conditional column rule keys belonging to this block
    rules_key = f"tr_cond_rules_{b}"
    for rule_id in st.session_state.pop(rules_key, []):
        st.session_state.pop(f"tr_cond_cond_{rule_id}", None)
        st.session_state.pop(f"tr_cond_val_{rule_id}", None)

def add_cond_rule(block_id):
    key = f"tr_cond_rules_{block_id}"
    if key not in st.session_state: st.session_state[key] = []
    st.session_state[key].append(str(uuid.uuid4()))

def remove_cond_rule(block_id, rule_id):
    key = f"tr_cond_rules_{block_id}"
    st.session_state[key] = [r for r in st.session_state.get(key, []) if r != rule_id]
    st.session_state.pop(f"tr_cond_cond_{rule_id}", None)
    st.session_state.pop(f"tr_cond_val_{rule_id}", None)

def ensure_choice(key, options, fallback):
    """Make session_state[key] a valid choice before its selectbox renders.

    Keyed selectboxes are rendered without index=: Streamlit warns when a
    widget has a default AND its value was set via session state, which is
    exactly what Load Configuration does. Instead, the default is written to
    session state here when the key is missing, and a stored value that is no
    longer among the options (e.g. a column absent from a new upload) is
    replaced by the fallback."""
    if st.session_state.get(key) not in options:
        st.session_state[key] = fallback


def retain_column_options(key, options, *, multiple=False):
    """Keep selected columns editable when preprocessing changes the options."""
    if key not in st.session_state:
        return options
    saved = st.session_state[key]
    selected = (saved or []) if multiple else ([saved] if saved else [])
    # Streamlit can reset a keyed widget when its options change. Reassert the
    # saved selection before rendering and keep it among the valid options.
    st.session_state[key] = saved
    return list(dict.fromkeys([*options, *selected]))


def missing_selected_columns(selected, available):
    return list(dict.fromkeys(col for col in selected if col and col not in available))


def missing_analysis_columns(groups, target, available):
    if not groups or not target:
        return []
    # Selecting (None) requests one analysis across all data.
    factors = [] if "(None)" in groups else groups
    return missing_selected_columns([target, *factors], available)


def unavailable_column_message(block, missing):
    return f"{block} is waiting for {', '.join(repr(col) for col in missing)}."


def condition_build_issue(block, error):
    if isinstance(error, _TooManyConditions):
        return (f"{block} would create {error.args[0]:,} conditions, above the limit of "
                f"{MAX_CONDITIONS_PER_BLOCK}. Choose categorical grouping columns or narrow the trial filter.")
    return f"{block} could not build its conditions: {type(error).__name__}: {error}"


def apply_trial_filter(frame, expression, cache):
    """Rows a valid trial filter keeps, or None when there is no usable filter.

    Invalid or unsafe filters return None: the analysis reports those itself.
    `cache` is a dict shared within one rerun, so each block's filter runs once
    for the summary, the no-trials check and the condition list.
    """
    expression = expression.strip()
    if not expression or not _check_expr_safety(expression):
        return None
    if expression not in cache:
        try:
            cache[expression] = frame.query(expression, local_dict={}, global_dict={})
        except Exception:
            cache[expression] = None
    return cache[expression]


def filter_matches_no_trials(frame, expression, cache):
    kept = apply_trial_filter(frame, expression, cache)
    return kept is not None and kept.empty


# --- TRIAL SUMMARIES ---
# "Unit" is one row of the results: a file, or a participant when files are
# merged by participant ID. Summaries follow the sidebar's grouping setting.
def analysis_unit(group_col, name_col):
    return "file" if group_col == name_col else "participant"


def unit_name(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def share_range(removed, total, unit, units):
    """'8.0–61.0% (highest: participant 7)' across units, or None for one unit."""
    removed, total = np.asarray(removed), np.asarray(total)
    eligible = np.flatnonzero(total > 0)
    if len(eligible) < 2:
        return None
    shares = removed[eligible] / total[eligible] * 100
    low, high = shares.min(), shares.max()
    if np.isclose(low, high):
        return f"{high:.1f}% for every {unit}"
    worst = units[eligible[int(np.argmax(shares))]]
    return f"{low:.1f}–{high:.1f}% (highest: {unit} {unit_name(worst)})"


def filter_summary(frame, expression, cache, unit_col, group_cols, unit, rt_col=''):
    """One line on what a block's trial filter removes, or None.

    Counts the trials the block can use, as the analysis does: rows with a
    unit and values in every grouping column, after excluding no-response
    trials for error-rate blocks (rt_col). Removing everything is reported
    by the no-trials warning instead.
    """
    kept = apply_trial_filter(frame, expression, cache)
    if kept is None:
        return None
    columns = [unit_col] + [c for c in group_cols if c != "(None)" and "(None)" not in group_cols]
    base = frame.dropna(subset=list(dict.fromkeys(columns)))
    if rt_col and rt_col in base.columns:
        base = base[pd.to_numeric(base[rt_col], errors='coerce').notna()]
    if base.empty:
        return None
    if base.index.is_unique and kept.index.is_unique:
        kept_base = base[base.index.isin(kept.index)]
    else:
        kept_base = base.query(expression, local_dict={}, global_dict={})
    total = base.groupby(unit_col).size()
    removed = total - kept_base.groupby(unit_col).size().reindex(total.index, fill_value=0)
    n_total, n_removed = int(total.sum()), int(removed.sum())
    if n_removed == n_total:
        return None
    if n_removed == 0:
        return f"The trial filter keeps all {n_total:,} trials."
    text = (f"The trial filter removes {n_removed:,} of {n_total:,} trials "
            f"({n_removed / n_total * 100:.1f}%).")
    spread = share_range(removed.to_numpy(), total.to_numpy(), unit, list(total.index))
    if spread:
        text += f" Removed per {unit}: {spread}."
    return text


def funnel_summary(funnel, mode, unit, show_counts):
    """Markdown per block: the trials each step removed, after a preview."""
    units = funnel["units"]
    kind = "Reaction Time" if mode == "RT" else "Error Rate"
    texts = []
    for block, fb in sorted(funnel["blocks"].items()):
        # (label, removed per unit, trials entering the step per unit, shown when 0)
        if mode == "RT":
            dv_label = "missing or zero values" if fb["exclude_zero"] else "missing values"
            steps = [("trial filter", fb["base"] - fb["post"], fb["base"], fb["has_filter"]),
                     (dv_label, fb["post"] - fb["kept_dv"], fb["post"], False),
                     ("outliers", fb["kept_dv"] - fb["final"], fb["kept_dv"], fb["outliers"])]
            end, end_label = fb["final"], "analysed"
        else:
            after_no_resp = fb["base"] - fb["no_resp"]
            steps = [("no-response trials", fb["no_resp"], fb["base"], fb["no_response"]),
                     ("trial filter", after_no_resp - fb["post"], after_no_resp, fb["has_filter"]),
                     ("values outside the denominator", fb["post"] - fb["den"], fb["post"], False)]
            end, end_label = fb["den"], "in the denominator"
        chain, spreads = [f"{int(fb['base'].sum()):,} trials"], []
        for label, removed, entering, always in steps:
            n_removed, n_entering = int(removed.sum()), int(entering.sum())
            if not n_removed and not always:
                continue
            share = f" ({n_removed / n_entering * 100:.1f}%)" if n_removed and n_entering else ""
            chain.append(f"{label} −{n_removed:,}{share}")
            spread = share_range(removed, entering, unit, units) if n_removed else None
            if spread:
                spreads.append(f"{label} {spread}")
        text = f"**{kind} block {block}:** " + " → ".join(chain) + f" → **{int(end.sum()):,} {end_label}**."
        if spreads:
            text += f"  \nRemoved per {unit}: " + "; ".join(spreads) + "."
        texts.append(text)
    if texts:
        texts.append("Details per " + unit + " and condition: "
                     + ("see the count columns in the results." if show_counts else
                        "tick *Include trial counts and outlier limits* in the sidebar."))
    return "\n\n".join(texts)


def no_trials_issue(block, expression):
    return (f"The trial filter in {block} ({expression.strip()}) matches no trials, so the block "
            "has nothing to analyse. Check the column names and values in the filter.")

# --- CONFIG HELPERS ---
def get_config_dict():
    """Gathers current session state for export."""
    state = st.session_state
    cfg = {key: state[key] for key in state if key.startswith('glob_')}

    # Build full keys from each block type's saved fields. Imported IDs can be
    # short, so suffix matching can capture unrelated state such as _pipeline_cache.
    block_fields = {
        'rt_blocks': (
            'rt_grp', 'rt_inc_col', 'rt_filt', 'rt_dv', 'rt_filt_txt', 'rt_out_en',
            'rt_out_sc', 'rt_out_mt', 'rt_out_th', 'rt_out_trim', 'rt_suf', 'rt_meas',
            'rt_excl_zero', 'rt_ms', 'rt_min_tr',
        ),
        'err_blocks': (
            'err_grp', 'err_inc', 'err_filt', 'err_col', 'err_num', 'err_den',
            'err_excl_to', 'err_ex', 'err_suf', 'err_pct', 'err_min_tr',
        ),
        'trans_blocks': (
            'tr_type', 'tr_name', 'tr_cola', 'tr_shift_amt', 'tr_op', 'tr_b',
            'tr_cond_rules', 'tr_cond_default', 'tr_cond_output_type', 'tr_formula',
            'tr_miss_fm', 'tr_miss_cols', 'tr_miss_nan', 'tr_miss_zero',
            'tr_miss_other_cb', 'tr_miss_other', 'tr_ext_fe', 'tr_ext_col',
            'tr_ext_min', 'tr_ext_max',
        ),
    }
    for structure, fields in block_fields.items():
        cfg[structure] = state[structure]
        for uid in cfg[structure]:
            for field in fields:
                key = f'{field}_{uid}'
                if key in state:
                    cfg[key] = state[key]

    for uid in cfg['trans_blocks']:
        for rule_id in cfg.get(f'tr_cond_rules_{uid}', []):
            for key in (f'tr_cond_cond_{rule_id}', f'tr_cond_val_{rule_id}'):
                if key in state:
                    cfg[key] = state[key]
    return cfg

# stlite: small, pure guards shared by file validation and direct application.
# Deliberately not a full settings schema: omitted fields and scalar semantics
# retain their existing behaviour. In particular, do not coerce research values.
def check_config_lists(cfg):
    if not isinstance(cfg, dict) or not all(isinstance(key, str) for key in cfg):
        raise ValueError("Configuration must be a JSON object. Settings were not applied.")

    block_labels = {'rt_blocks': 'Reaction-time blocks', 'err_blocks': 'Error-rate blocks',
                    'trans_blocks': 'Preprocessing blocks'}
    column_labels = {'glob_keep_cols': 'Metadata Columns in Results', 'glob_loops': 'Main loops'}
    list_prefixes = {'rt_grp_': 'Reaction-time grouping columns',
                     'err_grp_': 'Error-rate grouping columns',
                     'tr_miss_cols_': 'Missing-data columns',
                     'tr_cond_rules_': 'Recode rule IDs'}
    seen_ids = set()
    for key, values in cfg.items():
        label = block_labels.get(key) or column_labels.get(key)
        if label is None:
            label = next((text for prefix, text in list_prefixes.items() if key.startswith(prefix)), None)
        if label is None:
            continue
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{label} must be a list of text values. Settings were not applied.")
        if key in block_labels and len(values) > 1000:
            raise ValueError(f"{label} cannot contain more than 1,000 IDs. Settings were not applied.")
        if key in block_labels or key.startswith('tr_cond_rules_'):
            for uid in values:
                if not uid.strip():
                    raise ValueError(f"{label} must contain non-empty IDs. Settings were not applied.")
                if uid in seen_ids:
                    raise ValueError("Configuration contains repeated block or rule IDs. Settings were not applied.")
                seen_ids.add(uid)


def validate_and_load_config(cfg, available_cols):
    """
    Validates config against available columns.
    Returns:
    - cleaned_cfg: A dict safe to load (invalid keys removed).
    - missing_cols: A list of columns that were missing.
    """
    check_config_lists(cfg)  # stlite: reject invalid lists before column lookups
    cleaned_cfg = cfg.copy()
    missing = set()
    available_set = set(available_cols)

    # Columns created by the config's own New Column blocks count as available:
    # the preprocessing pipeline recreates them before any consumer (later
    # blocks, grouping, DV selectors) runs. Validating against the raw columns
    # alone would strip every reference to a derived column (e.g. grouping by
    # prev_task from a Shift block) and mislabel it as missing. Block order
    # is not checked here — a reference that the pipeline cannot satisfy at
    # runtime is skipped with a warning there.
    for uid in cfg.get('trans_blocks', []):
        derived = output_column_name(cfg.get(f"tr_type_{uid}"), cfg.get(f"tr_name_{uid}"))
        if derived:
            available_set.add(derived)

    # Derive valid loop names (clean versions) from the current dataset
    # This matches the sidebar logic: removing .thisN or _thisN
    loop_candidates = [c for c in available_cols if 'thisN' in c]
    valid_loop_names = {c.replace('_thisN', '').replace('.thisN', '') for c in loop_candidates}
    
    # Heuristic: Identify keys that likely hold column names based on naming convention
    keys_to_remove = []
    
    # 1. Global Participant Column
    if 'glob_part_col' in cleaned_cfg:
        v = cleaned_cfg['glob_part_col']
        if v != "(None)" and v not in available_set:
            missing.add(v)
            keys_to_remove.append('glob_part_col')

    # 2. Check Dynamic Keys (Single Strings)
    for k, v in cleaned_cfg.items():
        # Only check string values that are not empty or "(None)"
        if isinstance(v, str) and v and v != "(None)":
            # Check if this key corresponds to a column selector
            is_col_selector = False
            
            # Common patterns in our key naming
            if any(p in k for p in ['_dv_', '_cola_', '_col_']) and not k.startswith('tr_name'):
                is_col_selector = True
            # List-valued keys (glob_keep_cols, rt_grp_/err_grp_,
            # tr_miss_cols_) are validated in step 3 instead.

            if is_col_selector:
                if v not in available_set:
                    missing.add(v)
                    keys_to_remove.append(k)

    # 3. Handle Lists (Multiselects)
    # We need to filter the lists inside the config, not remove the whole key
    list_keys = [k for k in cleaned_cfg.keys() if k.startswith('glob_') or '_grp_' in k or k.startswith('tr_miss_cols_')]
    for k in list_keys:
        val = cleaned_cfg[k]
        if isinstance(val, list):
            # SPECIAL HANDLING FOR LOOPS
            # Config stores "clean" names (e.g., "trials"), so we validate against clean names
            if k == 'glob_loops':
                valid_items = [x for x in val if x in valid_loop_names]
            else:
                # Standard check: config value must match raw column name exactly
                valid_items = [x for x in val if x in available_set or x == "(None)"]
            
            if len(valid_items) < len(val):
                dropped = set(val) - set(valid_items)
                missing.update(dropped)
                cleaned_cfg[k] = valid_items

    # 4. Validate transformation Column B (only for binary ops; unary ops have no Column B)
    _unary_ops_v = {
        "Log (Natural: ln A)", "Log (Base 10: log10 A)",
        "Square Root (sqrt A)", "Inverse (1 / A)", "Absolute Value (|A|)"
    }
    for uid in cleaned_cfg.get('trans_blocks', []):
        b_key = f"tr_b_{uid}"
        op_key = f"tr_op_{uid}"
        if b_key in cleaned_cfg and op_key in cleaned_cfg:
            op = cleaned_cfg[op_key]
            val = cleaned_cfg[b_key]
            if isinstance(val, str) and val and op not in _unary_ops_v:
                if val not in available_set:
                    missing.add(val)
                    keys_to_remove.append(b_key)

    # Remove the invalid single-value keys
    for k in keys_to_remove:
        del cleaned_cfg[k]

    return cleaned_cfg, list(missing)

def apply_config(cfg):
    # stlite: direct kept-settings restoration uses the same pure checks.
    # Finish every check before the first session-state write.
    check_config_lists(cfg)

    # Restore Structures First
    if 'rt_blocks' in cfg: st.session_state.rt_blocks = cfg['rt_blocks']
    if 'err_blocks' in cfg: st.session_state.err_blocks = cfg['err_blocks']
    if 'trans_blocks' in cfg: st.session_state.trans_blocks = cfg['trans_blocks']
    # Older Recode configurations used Text even if the same block was later
    # switched to Number in this session.
    for uid in st.session_state.get('trans_blocks', []):
        output_key = f'tr_cond_output_type_{uid}'
        if cfg.get(f'tr_type_{uid}') == 'New Column — Recode Values' and output_key not in cfg:
            st.session_state.pop(output_key, None)
    
    # Restore Values — only keys matching known prefixes to prevent arbitrary injection.
    # Button keys are excluded: Streamlit disallows writing to button widget state.
    _valid_prefixes = ('glob_', 'rt_', 'err_', 'tr_')
    _button_prefixes = ('tr_up_', 'tr_dn_', 'tr_del_', 'tr_cond_add_', 'tr_cond_rm_',
                        'rt_del_', 'err_del_')
    for k, v in cfg.items():
        if (k not in ['rt_blocks', 'err_blocks', 'trans_blocks']
                and k.startswith(_valid_prefixes)
                and not k.startswith(_button_prefixes)):
            st.session_state[k] = v

# stlite: a dataset change is a single state transition, rather than letting
# individual widgets drop incompatible values independently during rendering.
def reset_analysis_settings():
    for key in list(st.session_state):
        if (key.startswith(('glob_', 'tr_', 'rt_', 'err_', 'config_uploader', '_diag_'))
                or key == 'trans_blocks'):
            st.session_state.pop(key, None)
    for key in ('rt_blocks', 'err_blocks', 'trans_blocks'):
        st.session_state[key] = [str(uuid.uuid4())]
    # A new widget key also clears the browser-side configuration-file selection.
    st.session_state._config_uploader_epoch = st.session_state.get('_config_uploader_epoch', 0) + 1


def settings_signature(config):
    """Compare actual choices independently of generated block/rule UUIDs."""
    identifiers = {}
    structures = ('rt_blocks', 'err_blocks', 'trans_blocks')
    for structure in structures:
        for index, uid in enumerate(config.get(structure, [])):
            identifiers[uid] = f'{structure}:{index}'
    for block in config.get('trans_blocks', []):
        for index, uid in enumerate(config.get(f'tr_cond_rules_{block}', [])):
            identifiers[uid] = f'{identifiers[block]}:rule:{index}'
    normalized = {}
    for key, value in config.items():
        normalized_key = key
        for uid, replacement in identifiers.items():
            if key.endswith('_' + uid):
                normalized_key = key[:-len(uid)] + replacement
                break
        if key in structures or key.startswith('tr_cond_rules_'):
            value = [identifiers.get(uid, uid) for uid in value]
        normalized[normalized_key] = value
    return json.dumps(normalized, sort_keys=True, default=str)


def remember_settings_baseline():
    # Capture after all first-render widget defaults have been initialised.
    # A kept setup retains the original baseline, so it is not mistaken for
    # an untouched setup on the following replacement.
    if st.session_state.get('_df_raw') is not None and '_settings_baseline' not in st.session_state:
        st.session_state._settings_baseline = settings_signature(get_config_dict())


def snapshot_dataset_settings():
    pending = st.session_state.get('_pending_dataset_change')
    if pending is not None:
        return copy.deepcopy(pending)
    # stlite: uploader reruns can interrupt a read without using Cancel. The
    # old frame and widget settings have already been released at that point.
    current = st.session_state.get('_current_upload_transition')
    if current is not None:
        return copy.deepcopy(current)
    frame = st.session_state.get('_df_raw')
    config = copy.deepcopy(get_config_dict()) if frame is not None else None
    baseline = st.session_state.get('_settings_baseline')
    return dict(replaced=st.session_state.get('_last_parsed_sig') is not None,
                settings=config, columns=sorted(frame.columns) if frame is not None else None,
                baseline=baseline,
                changed=config is not None and (baseline is None or settings_signature(config) != baseline))


def prepare_dataset_change(upload_sig):
    """Ask only for a replacement of a setup that the user has configured.

    Nothing is reset or parsed here. The plain configuration copy survives
    Streamlit cleaning up widgets that are hidden while awaiting a choice.
    """
    request = st.session_state.get('_replacement_request')
    if request is None:
        transition = snapshot_dataset_settings()
        request = dict(signature=upload_sig, transition=transition,
                       choice=None if transition['changed'] else 'reset')
        st.session_state._replacement_request = request
    elif request['signature'] != upload_sig:
        request['signature'] = upload_sig
        request['choice'] = None if request['transition']['changed'] else 'reset'
    return request


def choose_dataset_change(choice):
    if choice not in ('reset', 'keep'):
        raise ValueError('Unknown replacement choice')
    request = st.session_state.get('_replacement_request')
    if request is not None:
        request['choice'] = choice


def stage_removed_dataset():
    request = st.session_state.pop('_replacement_request', None)
    transition = request['transition'] if request is not None else snapshot_dataset_settings()
    # Removing a file frees its data, but preserves the small setup until the
    # replacement is selected and the user has had the opportunity to decide.
    release_dataset()
    st.session_state._pending_dataset_change = transition


def begin_dataset_change(transition=None, keep=False):
    """Apply a resolved choice before replacement controls are rendered."""
    transition = copy.deepcopy(transition if transition is not None else snapshot_dataset_settings())
    if not keep:
        transition.update(settings=None, columns=None, baseline=None, changed=False)
    st.session_state.pop('_replacement_request', None)
    st.session_state.pop('_pending_dataset_change', None)
    release_dataset()
    reset_analysis_settings()
    st.session_state.pop('_settings_baseline', None)
    st.session_state._current_upload_transition = transition
    return transition


def finish_dataset_change(transition, frame):
    """Restore an explicitly kept, compatible setup; never auto-apply a JSON file."""
    st.session_state.pop('_current_upload_transition', None)
    if frame is None:
        if transition['settings'] is not None:
            st.session_state._pending_dataset_change = transition
        return None
    if transition['settings'] is not None:
        if sorted(frame.columns) == transition['columns']:
            apply_config(transition['settings'])
            st.session_state._settings_baseline = transition['baseline']
            return "New data loaded. Your analysis settings were kept; previous results and the configuration-file selection were cleared."
        return "New data loaded with a different column layout. Analysis settings were reset; load a configuration for this dataset if needed."
    if transition['replaced']:
        return "New data loaded. All analysis settings were reset for a fresh analysis."
    return None


# stlite: one cached processed frame, including grouping-dependent counts,
# formula errors and replayable warnings. No pickled cache or historic frames.
def preprocessing_key(source_token, loop_columns, actual_group_col, name_col):
    ignored = ('tr_up_', 'tr_dn_', 'tr_del_', 'tr_cond_add_', 'tr_cond_rm_')
    state = {k: v for k, v in st.session_state.items()
             if k.startswith('tr_') and not k.startswith(ignored)}
    blocks = st.session_state.trans_blocks
    selected_participant_col = (st.session_state.get('glob_part_col')
                                if any(state.get(f'tr_type_{block}') in _NEW_COLUMN_TYPES
                                       for block in blocks) else None)
    return json.dumps([source_token, actual_group_col, name_col, loop_columns,
                       st.session_state.get('glob_use_min_rows', False),
                       st.session_state.get('glob_min_row_count', 100),
                       selected_participant_col, blocks, state], sort_keys=True, default=str)


def _derived_preprocessing_key(key):
    return key.startswith(('_pp_', '_formula_err_'))


def source_groups(frame, name_col, actual_group_col):
    groups = {'files': set(frame[name_col].unique().tolist())}
    if actual_group_col != name_col:
        groups['participant IDs'] = set(frame[actual_group_col].dropna().unique().tolist())
    return groups


def exclusion_messages(stage, excluded, *, limit=None):
    for kind, labels in excluded.items():
        if labels:
            names = ', '.join(repr(label) for label in labels[:limit])
            if limit is not None and len(labels) > limit:
                names += f' and {len(labels) - limit:,} more'
            label_type = kind.removesuffix('s') if len(labels) == 1 else kind
            yield f"{stage} left no trials for {len(labels):,} {label_type} ({names})."


def preprocess_cached(df_row_filtered, loop_cols_to_filter, actual_group_col,
                      name_col, source_token, *, raw_frame=None):
    signature = preprocessing_key(source_token, loop_cols_to_filter, actual_group_col, name_col)
    cached = st.session_state.get('_pipeline_cache')
    if cached is not None and cached['signature'] == signature:
        for key in list(st.session_state):
            if _derived_preprocessing_key(key):
                st.session_state.pop(key, None)
        st.session_state.update(cached['state'])
        st.session_state._diag_pipeline_hit = True
        return cached
    st.session_state.pop('_pipeline_cache', None)
    for key in list(st.session_state):
        if _derived_preprocessing_key(key):
            st.session_state.pop(key, None)
    messages = []
    _unary_ops = ["Log (Natural: ln A)", "Log (Base 10: log10 A)", "Square Root (sqrt A)", "Inverse (1 / A)", "Absolute Value (|A|)"]
    # 1. Start from sidebar-filtered base (min-rows already applied)
    current_df = df_row_filtered.copy(deep=False)  # stlite
    if raw_frame is None:
        raw_frame = df_row_filtered
    loaded_groups = source_groups(raw_frame, name_col, actual_group_col)
    minimum_groups = (loaded_groups if len(df_row_filtered) == len(raw_frame)
                      else source_groups(df_row_filtered, name_col, actual_group_col))

    # 2. Apply loop filter first (sidebar Step 2.2)
    if loop_cols_to_filter:
        mask = current_df[loop_cols_to_filter].notna().any(axis=1)
        current_df = current_df[mask]

    loop_rows = len(current_df)
    loop_groups = (minimum_groups if loop_rows == len(df_row_filtered)
                   else source_groups(current_df, name_col, actual_group_col))

    # 3. Capture per-participant totals BEFORE preprocessing blocks run
    st.session_state._pp_total_counts = (
        current_df.groupby(actual_group_col).size().to_dict() if not current_df.empty else {}
    )

    # 4. Run preprocessing blocks in user-defined order
    pp_missing_removed = {}   # {sub_id: rows removed by missing-data blocks}
    pp_extreme_removed = {}   # {sub_id: rows removed by extreme-value blocks}
    preprocessing_report = []  # All blocks in order (for settings report)
    unavailable_inputs = {}
    protected_outputs = {}
    arithmetic_errors = {}
    recode_errors = {}
    formula_infinities = {}
    shift_missing_ids = {}
    participant_col = st.session_state.get('glob_part_col')
    if participant_col in (name_col, '(None)') or participant_col not in df_row_filtered.columns:
        participant_col = None
    protected_columns = {name_col, actual_group_col, participant_col}

    for b_id in st.session_state.trans_blocks:
        block_type = st.session_state.get(f"tr_type_{b_id}")  # None if not yet chosen
        new_col = output_column_name(block_type, st.session_state.get(f'tr_name_{b_id}'))
        if new_col and new_col in protected_columns:
            protected_outputs[b_id] = new_col
            continue
        input_columns = current_df.columns
        selected = ()

        if block_type == "New Column — Shift Values":
            col_a_key = f"tr_cola_{b_id}"
            amt_key   = f"tr_shift_amt_{b_id}"
            col_a   = st.session_state.get(col_a_key, "")
            if new_col:
                selected = (col_a,)
            try: shift_val = int(st.session_state.get(amt_key, 1))
            except: shift_val = 1
            if new_col and col_a and col_a in current_df.columns:
                # Use original labels, aligned by trial index even after row removal.
                # An earlier transformation may have overwritten those columns.
                groupers = [df_row_filtered[name_col]]
                shift_participant_col = participant_col if shift_val else None
                missing_ids = 0
                if shift_participant_col is not None:
                    ids = df_row_filtered[shift_participant_col].reindex(current_df.index)
                    groupers.append(ids)
                    missing_ids = int(ids.isna().sum())
                try:
                    current_df[new_col] = current_df[col_a].groupby(groupers, sort=False).shift(shift_val)
                except Exception as e:
                    messages.append(f"⚠️ Shift block '{new_col}' failed and was skipped: {e}")
                else:
                    preprocessing_report.append({
                        "type": "New Column — Shift Values", "new_col_name": new_col,
                        "col_a": col_a, "shift_amt": shift_val,
                        "participant_column": shift_participant_col, "missing_participant_ids": missing_ids,
                    })
                    if missing_ids:
                        shift_missing_ids[b_id] = missing_ids

        elif block_type == "New Column — Arithmetic" and new_col:
            operation = st.session_state.get(f"tr_op_{b_id}", "Concatenate (A_B)")
            unary = operation in _unary_ops
            col_a = st.session_state.get(f"tr_cola_{b_id}", "")
            col_b = None if unary else st.session_state.get(f"tr_b_{b_id}")
            if missing := missing_selected_columns((col_a, col_b), input_columns):
                unavailable_inputs[b_id] = missing
                continue
            if not col_a or (not unary and not col_b):
                arithmetic_errors[b_id] = "Select all required input columns."
                continue
            try:
                current_df[new_col] = arithmetic_column(current_df, operation, col_a, col_b)
            except Exception as error:
                arithmetic_errors[b_id] = str(error) or type(error).__name__
            else:
                preprocessing_report.append({
                    "type": block_type, "new_col_name": new_col, "operation": operation,
                    "col_a": col_a, "col_b": col_b,
                })

        elif block_type == "New Column — Recode Values":
            rule_ids = st.session_state.get(f"tr_cond_rules_{b_id}", [])
            default  = st.session_state.get(f"tr_cond_default_{b_id}", "")
            rules = [
                (st.session_state.get(f"tr_cond_cond_{rid}", "").strip(),
                 st.session_state.get(f"tr_cond_val_{rid}", ""))
                for rid in rule_ids
                if st.session_state.get(f"tr_cond_cond_{rid}", "").strip()
            ]
            if new_col and rules:
                cfg = {
                    "type": "New Column — Recode Values",
                    "new_col_name": new_col,
                    "rules": rules,
                    "default": default,
                    "output_type": st.session_state.get(f"tr_cond_output_type_{b_id}", 'Text'),
                }
                try:
                    current_df = apply_conditional_column(current_df, cfg, warn=messages.append)
                except ValueError as error:
                    recode_errors[b_id] = str(error)
                else:
                    preprocessing_report.append(cfg)

        elif block_type == "New Column — Custom Formula":
            formula_key = f"tr_formula_{b_id}"
            err_key     = f"_formula_err_{b_id}"
            formula = st.session_state.get(formula_key, "").strip()
            if new_col and formula:
                cfg = {
                    "type": "New Column — Custom Formula",
                    "new_col_name": new_col,
                    "formula": formula,
                }
                try:
                    if not _check_expr_safety(formula, allow_math=True):
                        raise ValueError(
                            "Formula contains disallowed syntax or an oversized constant expression. "
                            "Use column names, operators, and one argument per maths function "
                            f"({', '.join(_FORMULA_MATH_FUNCTIONS)})."
                        )
                    # stlite: engine=None (pandas' default) instead of 'numexpr': Pyodide has no
                    # numexpr, so pandas uses its pure-Python engine. Safety never came from
                    # the engine but from _check_expr_safety above.
                    current_df[new_col] = current_df.eval(formula, engine=None, local_dict={})
                except Exception as e:
                    st.session_state[err_key] = str(e) or type(e).__name__
                else:
                    st.session_state[err_key] = None
                    values = current_df[new_col]
                    positive = int(values.isin([np.inf]).sum())
                    negative = int(values.isin([-np.inf]).sum())
                    if positive or negative:
                        cfg['infinity_counts'] = formula_infinities[b_id] = (positive, negative)
                    preprocessing_report.append(cfg)
            else:
                st.session_state.pop(f"_formula_err_{b_id}", None)

        elif block_type == "Missing Data Removal":
            cols        = st.session_state.get(f"tr_miss_cols_{b_id}", [])
            remove_nan  = st.session_state.get(f"tr_miss_nan_{b_id}", True)
            remove_zero = st.session_state.get(f"tr_miss_zero_{b_id}", False)
            use_other   = st.session_state.get(f"tr_miss_other_cb_{b_id}", False)
            other_text  = st.session_state.get(f"tr_miss_other_{b_id}", "") if use_other else ""

            # Parse comma-separated "other" values into Python scalars
            other_vals = []
            for part in other_text.split(','):
                part = part.strip()
                if not part:
                    continue
                try:
                    try:    other_vals.append(int(part))
                    except ValueError: other_vals.append(float(part))
                except ValueError:
                    other_vals.append(part)  # keep as string; matched exactly

            active = remove_nan or remove_zero or bool(other_vals)
            selected = cols if active else ()
            if selected and not missing_selected_columns(selected, input_columns):
                n_before = current_df.groupby(actual_group_col).size()
                mask = pd.Series([True] * len(current_df), index=current_df.index)
                for col in cols:
                    col_mask = pd.Series([True] * len(current_df), index=current_df.index)
                    numeric  = pd.to_numeric(current_df[col], errors='coerce')
                    if remove_nan:
                        col_mask = col_mask & current_df[col].notna()
                    if remove_zero:
                        col_mask = col_mask & (numeric != 0)
                    for val in other_vals:
                        if isinstance(val, (int, float)):
                            col_mask = col_mask & ~(numeric == val)
                        else:
                            col_mask = col_mask & (current_df[col].astype(str).str.strip() != str(val))
                    mask = mask & col_mask
                current_df = current_df[mask]
                n_after = current_df.groupby(actual_group_col).size()
                # sub(fill_value=0) so a participant whose every row was removed (and is
                # therefore absent from n_after) counts as fully removed instead of
                # becoming NaN. Plain (n_before - n_after) aligns the missing label to
                # NaN, and the later int(cnt) then raises ValueError, crashing the whole
                # top-level pipeline run (the tabs never render, so the offending block
                # can't even be edited). reindex(fill_value=) does NOT fix this: the
                # label already exists with a NaN value, so fill_value never applies.
                removed = n_before.sub(n_after, fill_value=0).astype(int)
                for sid, cnt in removed.items():
                    pp_missing_removed[sid] = pp_missing_removed.get(sid, 0) + int(cnt)
                st.session_state[f"_pp_count_{b_id}"] = (int(n_before.sum()), int(n_after.sum()))
                criteria = (["NaN"] if remove_nan else []) + (["0"] if remove_zero else []) + \
                           ([f"other: {', '.join(str(v) for v in other_vals)}"] if other_vals else [])
                preprocessing_report.append({"type": "Missing Data Removal", "cols": cols, "criteria": criteria})
            else:
                st.session_state.pop(f"_pp_count_{b_id}", None)

        elif block_type == "Extreme Value Rejection":
            col   = st.session_state.get(f"tr_ext_col_{b_id}", "")
            selected = (col,)
            min_v = st.session_state.get(f"tr_ext_min_{b_id}", 0.15)
            max_v = st.session_state.get(f"tr_ext_max_{b_id}", 4.0)
            if col and col in current_df.columns:
                n_before = current_df.groupby(actual_group_col).size()
                v = pd.to_numeric(current_df[col], errors='coerce')
                mask = (v.isna()) | ((v >= min_v) & (v <= max_v))
                current_df = current_df[mask]
                n_after = current_df.groupby(actual_group_col).size()
                # See Missing Data Removal above: sub(fill_value=0) avoids the int(NaN)
                # crash when a participant's every row falls outside the kept range.
                removed = n_before.sub(n_after, fill_value=0).astype(int)
                for sid, cnt in removed.items():
                    pp_extreme_removed[sid] = pp_extreme_removed.get(sid, 0) + int(cnt)
                st.session_state[f"_pp_count_{b_id}"] = (int(n_before.sum()), int(n_after.sum()))
                preprocessing_report.append({"type": "Extreme Value Rejection", "col": col, "min_v": min_v, "max_v": max_v})
            else:
                st.session_state.pop(f"_pp_count_{b_id}", None)

        missing = missing_selected_columns(selected, input_columns)
        if missing:
            unavailable_inputs[b_id] = missing

    st.session_state._pp_missing_removed = pp_missing_removed
    st.session_state._pp_extreme_removed = pp_extreme_removed

    # 5. Update column list so new columns appear in dropdowns
    all_cols = sorted(list(current_df.columns))
    rt_candidates = [c for c in all_cols if c.endswith('_rt') or c.endswith('.rt')]
    corr_candidates = [c for c in all_cols if c.endswith('.corr') or c.endswith('_corr')]

    df_main_process = current_df

    remaining_groups = loop_groups
    if len(current_df) < loop_rows:
        # Row indices still identify the source trials. Read their original
        # labels in case a transformation overwrote a grouping column.
        group_cols = list(dict.fromkeys([name_col, actual_group_col]))
        remaining_groups = source_groups(df_row_filtered.loc[current_df.index, group_cols],
                                         name_col, actual_group_col)
    stages = [('Minimum row filter', loaded_groups, minimum_groups),
              ('Main loop filter', minimum_groups, loop_groups),
              ('Preprocessing blocks', loop_groups, remaining_groups)]
    retention = {
        'loaded': {kind: len(labels) for kind, labels in loaded_groups.items()},
        'remaining': {kind: len(labels) for kind, labels in remaining_groups.items()},
        'excluded': {stage: {kind: sorted(labels - after[kind], key=repr)
                             for kind, labels in before.items()}
                     for stage, before, after in stages},
    }
    cached = dict(signature=signature, frame=df_main_process, report=preprocessing_report,
                  loop_rows=loop_rows, warnings=messages, unavailable_inputs=unavailable_inputs,
                  protected_outputs=protected_outputs,
                  retention=retention, shift_missing_ids=shift_missing_ids,
                  arithmetic_errors=arithmetic_errors, recode_errors=recode_errors,
                  formula_infinities=formula_infinities,
                  state={k: v for k, v in st.session_state.items() if _derived_preprocessing_key(k)})
    st.session_state._pipeline_cache = cached
    st.session_state._diag_pipeline_hit = False
    return cached


# ==========================================
# 4. SIDEBAR CONFIGURATION
# ==========================================
st.set_page_config(layout="wide", page_title="Francis")

st.markdown(
    """
    <style>
        /* Only apply minimum width when the sidebar is OPEN */
        [data-testid="stSidebar"][aria-expanded="true"] {
            min-width: 400px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# stlite: track_visit() call removed (see the top of the file).

def _render_about(build):
    # Each build links to its own tag on GitHub, which is pushed before the
    # build is published (release_tools/copy_to_github.py).
    repository = "https://github.com/jderrfuss/francis"
    if build and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", build):
        source = (f"[The source code of this version (build {build})]({repository}/tree/{build}) "
                  "is on GitHub, together with instructions for running Francis on your own computer.")
    else:
        source = (f"[The source code]({repository}) is on GitHub, together with instructions "
                  "for running Francis on your own computer.")
    st.markdown("""
    <div style="max-width:750px;">
    
    Francis is a web app for preprocessing and collating PsychoPy CSV output files into a single, analysis-ready spreadsheet. If you have used pivot tables in Excel to aggregate your data by condition, Francis does the same — but with a full preprocessing pipeline built in: missing data removal, extreme value rejection, outlier exclusion, and flexible accuracy calculations — all without writing a line of code.
                
    Only need to collate data without further preprocessing? Try [Paco](https://paco.streamy.psychology.nottingham.ac.uk/).

    #### How to Cite
    
    If you use this app for your research or coursework, please cite it as follows:

    Derrfuss, J. (2026). *Francis: Advanced data preprocessing and collation* [Computer software]. https://francis.streamy.psychology.nottingham.ac.uk/

    #### Contact & Feedback

    Found a bug? Have a suggestion? [Email Jan](mailto:jan.derrfuss@nottingham.ac.uk)

    #### Source Code

    Francis is free software (AGPL-3.0).

    </div>
    """, unsafe_allow_html=True)
    st.markdown(source)

def _inject_sticky_tabs():
    """
    JS polyfill: sticky tab bar + per-tab scroll-position memory.

    Sticky bar: position:fixed with a ghost placeholder, driven by scroll
    events on section.stMain.  A ResizeObserver on scrollEl recalculates the
    bar's position whenever the sidebar opens/closes (which resizes stMain).

    Scroll memory:
    - Positions are stored in w._fgScrollMem (a plain object on window.parent)
      so they survive iframe reloads caused by the rerun counter.
    - The PRIMARY save+restore happens in the pointerdown handler on document.body,  (stlite: was mousedown)
      which fires before every tab click regardless of Python reruns.
    - savePos() (on scroll) keeps positions current while the user scrolls.
    - applyRestore() sets scrollTop and retries for up to 3 s to counteract
      React silently clamping scrollTop to 0 when DOM height collapses.
    - A generation counter cancels stale retry loops when a new click arrives.
    """
    if '_sticky_rc' not in st.session_state:
        st.session_state._sticky_rc = 0
    st.session_state._sticky_rc += 1
    _rc = st.session_state._sticky_rc
    components.html(f"""<script>
/* rc={_rc} */
(function () {{
    var d = window.parent.document;
    var w = window.parent;
    // stlite: Streamlit 1.62 (bundled with stlite) draws tabs with React Aria
    // instead of BaseWeb, so the data-baseweb attributes are gone. Match both:
    // role="tablist"/"tab" is what React Aria sets.
    var SEL_BAR = '[data-baseweb="tab-list"], [role="tablist"]';
    var SEL_TAB = '[data-baseweb="tab"], [role="tab"]';
    var SEL_ACTIVE = '[data-baseweb="tab"][aria-selected="true"], [role="tab"][aria-selected="true"]';

    // ── Clean up from previous Streamlit re-render ──────────────────────
    if (w._fgListener && w._fgScrollEl)
        w._fgScrollEl.removeEventListener('scroll', w._fgListener);
    if (w._fgScrollSaver && w._fgScrollEl)
        w._fgScrollEl.removeEventListener('scroll', w._fgScrollSaver);
    if (w._fgMouseSaver)
        d.body.removeEventListener('pointerdown', w._fgMouseSaver, true);  // stlite: was mousedown
    if (w._fgResizeObserver)
        w._fgResizeObserver.disconnect();
    if (w._fgTabTrackInterval)
        clearInterval(w._fgTabTrackInterval);
    // Do NOT disconnect _fgTabResetObserver here — keeping it alive across
    // iframe reloads means it can catch React's aria-selected reset the instant
    // it happens, before the new IIFE has had a chance to set up its own observer.
    // The handler it calls (_fgTabResetHandler) is updated below with fresh closures.
    if (w._fgGhost && w._fgGhost.parentNode)
        w._fgGhost.parentNode.removeChild(w._fgGhost);
    if (w._fgShield && w._fgShield.parentNode)
        w._fgShield.parentNode.removeChild(w._fgShield);
    var prevBar = d.querySelector(SEL_BAR);
    if (prevBar) prevBar.removeAttribute('style');

    // ── Scroll container ─────────────────────────────────────────────────
    var scrollEl = d.querySelector('section.stMain') ||
                   d.querySelector('[data-testid="stMain"]');
    if (!scrollEl) return;
    w._fgScrollEl = scrollEl;

    // ── Tab-reset detector ────────────────────────────────────────────────
    // Fires the instant React changes aria-selected (e.g. when new widgets
    // appear in a tab panel and Streamlit resets the active tab to index 0).
    // Re-clicks the user's tab immediately so the flash is imperceptible.
    //
    // The observer is created ONCE and never disconnected across iframe reloads
    // (see cleanup comment above).  The actual handler logic lives in
    // w._fgTabResetHandler so each reload can update the closure (myGen,
    // applyRestore, etc.) without recreating the observer.
    w._fgTabResetHandler = function() {{
        if (w._fgSkipMouseSave || !w._fgCurrentTab || w._fgRestoring) return;
        var currentKey = activeTabKey();
        if (!currentKey || currentKey === w._fgCurrentTab) return;
        var targetTab = w._fgCurrentTab;
        var allTabs = d.querySelectorAll(SEL_TAB);
        for (var i = 0; i < allTabs.length; i++) {{
            if ('fsc_' + allTabs[i].textContent.trim().slice(0, 40) === targetTab) {{
                w._fgSkipMouseSave = true;
                allTabs[i].click();
                var capturedGen = myGen;
                setTimeout(function() {{
                    w._fgSkipMouseSave = false;
                    var scrollTarget = (w._fgScrollMem[targetTab] != null) ? w._fgScrollMem[targetTab] : 0;
                    applyRestore(scrollTarget, capturedGen);
                }}, 100);
                break;
            }}
        }}
    }};
    if (!w._fgTabResetObserver) {{
        w._fgTabResetObserver = new MutationObserver(function() {{
            if (w._fgTabResetHandler) w._fgTabResetHandler();
        }});
        w._fgTabResetObserver.observe(scrollEl, {{ subtree: true, attributes: true, attributeFilter: ['aria-selected'] }});
    }}

    // ── Scroll-position memory ────────────────────────────────────────────
    if (!w._fgScrollMem) w._fgScrollMem = {{}};
    w._fgRestoreGen = (w._fgRestoreGen || 0) + 1;
    var myGen = w._fgRestoreGen;
    w._fgRestoring = false;

    function activeTabKey() {{
        var t = d.querySelector(SEL_ACTIVE);
        return t ? 'fsc_' + t.textContent.trim().slice(0, 40) : null;
    }}
    function savePos() {{
        if (w._fgRestoring) return;
        var k = activeTabKey();
        if (k) w._fgScrollMem[k] = scrollEl.scrollTop;
    }}

    // applyRestore: set scrollTop and keep reapplying for a guaranteed window
    // so that late React rendering passes (which silently clamp scrollTop to 0)
    // are caught even after the position first looks stable.
    // Always clears _fgRestoring on exit so savePos is never left blocked.
    function applyRestore(target, gen) {{
        if (w._fgRestoreGen !== gen) {{ w._fgRestoring = false; return; }}
        scrollEl.scrollTop = target;
        if (target === 0) {{
            // Hold _fgRestoring=true for 250 ms so the scroll event produced by
            // setting scrollTop=0 — which arrives while aria-selected still shows
            // the departing tab — cannot overwrite that tab's saved position via
            // savePos.  React typically commits aria-selected within ~150 ms.
            setTimeout(function() {{
                if (w._fgRestoreGen === gen) w._fgRestoring = false;
            }}, 250);
            return;
        }}
        w._fgRestoring = true;
        var start = Date.now();
        function keepAlive() {{
            if (w._fgRestoreGen !== gen) {{ w._fgRestoring = false; return; }}
            if (Date.now() - start > 500) {{ w._fgRestoring = false; return; }}
            if (scrollEl.scrollTop < target - 5) scrollEl.scrollTop = target;
            setTimeout(keepAlive, 80);
        }}
        setTimeout(keepAlive, 80);
    }}

    // restorePos: runs on Streamlit reruns (iframe reload). Tab clicks do
    // NOT trigger reruns so this fires once at initial load; mouseSave
    // handles all subsequent tab navigation.
    // If Streamlit reset the active tab (e.g. new widgets appeared in a panel),
    // we detect the mismatch and re-click the tab the user was on.
    function restorePos() {{
        var k = w._fgPendingKey || w._fgCurrentTab || activeTabKey();
        w._fgPendingKey = null;
        if (!k) return;
        var currentKey = activeTabKey();
        if (currentKey && currentKey !== k) {{
            // Streamlit reset the tab — find and re-click the correct one
            var allTabs = d.querySelectorAll(SEL_TAB);
            for (var i = 0; i < allTabs.length; i++) {{
                if ('fsc_' + allTabs[i].textContent.trim().slice(0, 40) === k) {{
                    w._fgSkipMouseSave = true;
                    allTabs[i].click();
                    var scrollTarget = (w._fgScrollMem[k] != null) ? w._fgScrollMem[k] : 0;
                    var capturedGen = myGen;
                    setTimeout(function() {{
                        w._fgSkipMouseSave = false;
                        applyRestore(scrollTarget, capturedGen);
                    }}, 200);
                    return;
                }}
            }}
        }}
        var target = (w._fgScrollMem[k] != null) ? w._fgScrollMem[k] : 0;
        applyRestore(target, myGen);
    }}

    w._fgScrollSaver = savePos;
    scrollEl.addEventListener('scroll', savePos, {{ passive: true }});
    setTimeout(restorePos, 300);

    // Track active tab continuously so restorePos can detect Streamlit-triggered resets
    w._fgTabTrackInterval = setInterval(function() {{
        if (!w._fgRestoring) {{
            var k = activeTabKey();
            if (k) w._fgCurrentTab = k;
        }}
    }}, 1000);

    // ── Setup: sticky bar + pointerdown save ─────────────────────────────
    function setup() {{
        var bar = d.querySelector(SEL_BAR);
        if (!bar) {{ setTimeout(setup, 200); return; }}

        function mouseSave(e) {{
            if (w._fgSkipMouseSave) return;
            var currentBar = d.querySelector(SEL_BAR);
            if (!currentBar || !currentBar.contains(e.target)) return;
            var k = activeTabKey();
            if (k) w._fgScrollMem[k] = scrollEl.scrollTop;
            var dest = e.target.closest(SEL_TAB);
            if (!dest) return;
            var destKey = 'fsc_' + dest.textContent.trim().slice(0, 40);
            w._fgPendingKey = destKey;
            w._fgCurrentTab = destKey;
            var target = (w._fgScrollMem[destKey] != null) ? w._fgScrollMem[destKey] : 0;
            w._fgRestoring = true;
            w._fgRestoreGen += 1;
            var gen = w._fgRestoreGen;
            setTimeout(function() {{ applyRestore(target, gen); }}, 100);
        }}
        w._fgMouseSaver = mouseSave;
        // stlite: pointerdown, not mousedown. React Aria tabs (Streamlit 1.62)
        // switch on pointerdown, which comes before mousedown. With mousedown,
        // the tab-reset observer saw the user's own switch first, took it for
        // a Streamlit reset and clicked the previous tab again, so mouse
        // clicks could not change tabs at all. A capture listener on <body>
        // runs before React's own listeners on its root, and pointerdown also
        // precedes BaseWeb's click handling, so this works on 1.50 too.
        d.body.addEventListener('pointerdown', mouseSave, true);

        var bg = w.getComputedStyle(d.documentElement)
                    .getPropertyValue('--background-color').trim() || '#ffffff';

        // Ghost preserves layout space while bar is position:fixed.
        // Insert it first so we can measure the bar's natural document position.
        var ghost = d.createElement('div');
        bar.parentNode.insertBefore(ghost, bar);
        w._fgGhost = ghost;

        // HEADER = bar's natural y-position from viewport when scrollTop = 0.
        // Measured as ghost's current viewport-top + current scrollTop so the
        // result is scroll-independent.  Replaces stHeader.offsetHeight (which
        // was 60 in dev / 0 in deployed and caused a 44px jump in dev plus a
        // visible gap above the bar in deployed).
        var HEADER = ghost.getBoundingClientRect().top + scrollEl.scrollTop;

        // Shield: a fixed element covering viewport 0 → HEADER px.  When the
        // bar is fixed, the shield prevents tab-panel content from scrolling
        // into the empty strip above the bar (the block-container top padding).
        // pointer-events:none so it never blocks clicks.
        var shield = d.createElement('div');
        shield.style.cssText = 'position:fixed;top:0;height:0;z-index:9998;pointer-events:none;background:' + bg;
        d.body.appendChild(shield);
        w._fgShield = shield;

        function onScroll() {{
            // Always re-query the live bar — React may have replaced the element
            var liveBar = d.querySelector(SEL_BAR);
            if (!liveBar) return;
            // Re-insert ghost if React detached it
            if (!ghost.isConnected) {{
                liveBar.parentNode.insertBefore(ghost, liveBar);
            }}
            var gRect = ghost.getBoundingClientRect();
            if (gRect.top <= HEADER) {{
                ghost.style.height            = liveBar.offsetHeight + 'px';
                liveBar.style.position        = 'fixed';
                liveBar.style.top             = HEADER + 'px';
                liveBar.style.left            = gRect.left + 'px';
                liveBar.style.width           = ghost.offsetWidth + 'px';
                liveBar.style.zIndex          = '9999';
                liveBar.style.backgroundColor = bg;
                liveBar.style.boxShadow       = '0 2px 4px -2px rgba(0,0,0,.12)';
                shield.style.left             = gRect.left + 'px';
                shield.style.width            = ghost.offsetWidth + 'px';
                shield.style.height           = HEADER + 'px';
            }} else {{
                ghost.style.height  = '0';
                shield.style.height = '0';
                liveBar.removeAttribute('style');
            }}
        }}
        w._fgListener = onScroll;
        scrollEl.addEventListener('scroll', onScroll, {{ passive: true }});

        // ResizeObserver fires when stMain resizes (sidebar open/close),
        // keeping the fixed bar's left/width in sync with the new layout.
        var ro = new w.ResizeObserver(function() {{ onScroll(); }});
        ro.observe(scrollEl);
        w._fgResizeObserver = ro;

        onScroll();
    }}
    setTimeout(setup, 150);
}})();
</script>""", height=0)


def _render_guide():
    """Render user_guide.md with a top TOC (top-level sections only) and capped line width."""
    try:
        _guide_path = os.path.join(os.path.dirname(__file__), "user_guide.md")
        with open(_guide_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        st.warning("user_guide.md not found.")
        return

    # Inject anchors for all headings; collect only ## for the TOC
    toc_entries = []
    processed_lines = []
    for line in raw.split('\n'):
        m = re.match(r'^(#{2,3}) (.+)$', line)
        if m:
            level = len(m.group(1))
            heading = m.group(2)
            slug = re.sub(r'[^\w\s-]', '', heading.lower())
            slug = re.sub(r'[\s_]+', '-', slug).strip('-')
            if level == 2:
                toc_entries.append((heading, slug))
            processed_lines.append(f'\n<a id="{slug}"></a>\n\n{line}')
        else:
            processed_lines.append(line)

    # TOC as a compact inline list
    links = ' &nbsp;·&nbsp; '.join(
        f'<a href="#{slug}" style="text-decoration:none;white-space:nowrap;">{heading}</a>'
        for heading, slug in toc_entries
    )
    toc_html = (
        f'<div style="max-width:740px;font-size:0.9em;line-height:2;'
        f'border-bottom:1px solid rgba(49,51,63,0.15);padding-bottom:0.75em;margin-bottom:1.5em;">'
        f'<strong>Contents:</strong> &nbsp;{links}</div>'
    )

    # Cap line width for comfortable reading.
    # scroll-margin-top offsets anchor targets below the Streamlit header bar.
    content_html = (
        '<style>a[id] { scroll-margin-top: 4rem; }</style>'
        '<div style="max-width:740px;">'
        + '\n'.join(processed_lines)
        + '</div>'
    )

    st.markdown(toc_html + content_html, unsafe_allow_html=True)

# --- GLOBAL VARIABLES INIT ---
# stlite: session_state owns live datasets; script-local aliases are disposable.
for _data_alias in _DATA_ALIASES:
    globals().pop(_data_alias, None)
df_raw = None
name_col = 'file_name'  # bookkeeping filename column (load_data may pick a fallback)
run_analysis_clicked = False  # set by the sidebar button; consumed by the download logic
_waiting_for_replacement = False  # stlite: no analysis against an undecided selection
df_processed = None # New variable for transformed data
actual_group_col = None
all_cols = [] 
loop_cols = [] # Actual col names selected
keep_cols = []
glob_settings = {}
trans_active_list = [] # List to store active transformations

# --- SIDEBAR PLACEHOLDERS ---
slot_loops_count = None
slot_min_excluded = None
slot_nan_ids = None
runtime_versions = report_runtime_versions()

with st.sidebar:
    st.markdown('<h1 style="color:#FF4B4B;font-size:4.2rem;margin-bottom:0;line-height:1.1;">Francis</h1>', unsafe_allow_html=True)
    st.markdown('<p style="font-size:1.2rem;margin-top:0.1rem;color:#444;">Advanced Data Preprocessing and Collation</p>', unsafe_allow_html=True)
    if build := runtime_versions.get('Francis'):
        st.caption(f"Build {build}")
    st.divider()
    _notice = st.session_state.pop('_operation_notice', None)  # stlite
    if _notice:
        st.info(_notice)
    st.header("1. Input Data")

    # stlite: the "anonymised data only" notice and confirmation checkbox are
    # replaced (Paco's wording), because the file is never sent anywhere: it is
    # read by Python running inside this browser tab.
    st.info(
        "🔒 **Privacy:** Your files stay on your computer. Francis runs entirely inside "
        "your web browser, so the files you select are processed locally and are "
        "never uploaded to a server."
    )
    uploaded_file = st.file_uploader(
        "Upload Data", type=['zip', 'csv'],
        label_visibility="collapsed",
        key=f"data_upload_{st.session_state.get('_data_uploader_key', 0)}",  # stlite
        help="Upload a single CSV file or a ZIP file containing multiple CSV files."
    )
    
    if uploaded_file:
        # Streamlit reruns the whole script on every widget interaction, so
        # re-parsing the (up to 200MB) upload each time would make the tabs
        # sluggish. Identify the upload by a content hash and cache the parsed
        # result in session state, re-parsing only when the bytes change. The
        # hash is recomputed only when file_id changes (a fresh value per
        # uploaded file), not on every rerun.
        if st.session_state.get("_upload_file_id") != uploaded_file.file_id:
            _file_bytes = uploaded_file.getvalue()
            st.session_state._upload_file_id = uploaded_file.file_id
            st.session_state._upload_sig = (
                uploaded_file.name,
                uploaded_file.size,
                hashlib.sha256(_file_bytes).hexdigest(),
            )
        upload_sig = st.session_state._upload_sig

        # Parse only when the signature changes. The result (including a None
        # for a rejected/unreadable upload) is recorded so a failed file is not
        # re-parsed on every rerun.
        if st.session_state.get("_last_parsed_sig") != upload_sig:
            _replacement = prepare_dataset_change(upload_sig)
            if _replacement['choice'] is None:
                st.info("New dataset selected. Keep your current settings?")
                _reset_col, _keep_col = st.columns(2)
                with _reset_col:
                    st.button("Reset settings", key='replacement_reset', type='primary',
                              on_click=choose_dataset_change, args=('reset',))
                with _keep_col:
                    st.button("Keep settings", key='replacement_keep',
                              on_click=choose_dataset_change, args=('keep',))
                _waiting_for_replacement = True
            else:
                _upload_transition = begin_dataset_change(
                    _replacement['transition'], keep=_replacement['choice'] == 'keep')
                _t_parse = time.perf_counter()
                _df_raw, _name_col, _load_msgs = await run_with_progress(
                    load_data_steps(uploaded_file), "cancel_upload", cancel_upload)
                st.session_state._diag_parse = (time.perf_counter() - _t_parse,
                                                uploaded_file.size / (1024 * 1024))
                st.session_state._last_parsed_sig = upload_sig
                st.session_state._df_raw = _df_raw
                st.session_state._name_col = _name_col
                st.session_state._load_msgs = _load_msgs
                _change_notice = finish_dataset_change(_upload_transition, _df_raw)
                if _change_notice:
                    st.info(_change_notice)
        elif '_replacement_request' in st.session_state:
            # The original file was reselected while a replacement was pending.
            _abandoned = st.session_state.pop('_replacement_request')
            if _abandoned['transition']['settings'] is not None:
                apply_config(_abandoned['transition']['settings'])

        df_raw = None if _waiting_for_replacement else st.session_state.get("_df_raw")
        name_col = st.session_state.get("_name_col", "file_name")

        # Keep upload warnings visible across reruns, even when there are many
        # routine inference notices for this upload.
        _load_msgs = [] if _waiting_for_replacement else (st.session_state.get("_load_msgs") or [])
        upload_warnings = []
        inference_notices = {kind: [] for kind in _CSV_INFERENCE_LABELS}
        for kind, message in _load_msgs:
            if kind == 'error':
                st.error(message)
            elif kind in inference_notices:
                inference_notices[kind].append(message)
            else:
                upload_warnings.append(message)
        if upload_warnings:
            st.warning('\n\n'.join(upload_warnings))

        summaries = []
        for kind, notices in inference_notices.items():
            if notices:
                unit = 'column' if len(notices) == 1 else 'columns'
                summaries.append(f"{_CSV_INFERENCE_LABELS[kind]} in {len(notices):,} {unit}.")
        if summaries:
            st.caption(' '.join(summaries))
            with st.expander("CSV parsing details", expanded=False):
                for kind, notices in inference_notices.items():
                    if notices:
                        details = '\n'.join(f'- {notice}' for notice in notices[:200])
                        st.markdown(f"**{_CSV_INFERENCE_LABELS[kind]}**\n\n{details}")
                        if len(notices) > 200:
                            st.caption(f"{len(notices) - 200:,} more in the settings report.")

        if df_raw is not None:
            st.caption(f"Raw rows loaded: {len(df_raw)}")
            raw_cols = sorted(list(df_raw.columns))
            all_cols = raw_cols  # Establish for validation
            rt_candidates = [c for c in raw_cols if c.endswith('_rt') or c.endswith('.rt')]
            has_rt = len(rt_candidates) > 0

            # --- CONFIG MANAGER (only visible once data is loaded) ---
            st.markdown("---")
            # Keep the panel open while a config file is loaded. Dropping a file
            # triggers a rerun, and a hardcoded expanded=False would snap the
            # expander shut before the user can click Apply Settings. Read the
            # uploader's state (via its key) before the expander so we keep it
            # expanded whenever a file is pending.
            _cfg_uploader_key = f"config_uploader_{st.session_state.get('_config_uploader_epoch', 0)}"  # stlite
            _cfg_pending = st.session_state.get(_cfg_uploader_key) is not None
            with st.expander("📂 Load Configuration", expanded=_cfg_pending):
                st.caption("Use a Francis configuration from a source you trust. "
                           "Review the loaded settings before running an analysis.")
                uploaded_config = st.file_uploader("Upload Settings (.json)", type=['json'], key=_cfg_uploader_key)
                if uploaded_config:
                    _cfg_applied = st.session_state.get('_applied_config_file_id') == uploaded_config.file_id
                    try:
                        data = json.load(uploaded_config)
                        clean_data, missing = validate_and_load_config(data, raw_cols)
                        
                        if missing:
                            st.warning(f"⚠️ **Partial match:** The following columns in the config are missing from your data and were skipped:\n\n{', '.join(missing)}")
                        elif not _cfg_applied:
                            st.caption("✓ Settings validated — click Apply Settings to load them.")

                        if st.button("Apply Settings", type="primary"):
                            apply_config(clean_data)
                            # Keep feedback through the rerun, tied to this selected
                            # file so another upload cannot inherit its confirmation.
                            st.session_state._applied_config_file_id = uploaded_config.file_id
                            st.rerun()
                        if _cfg_applied:
                            st.success("Available settings applied successfully. Missing columns were skipped."
                                       if missing else "Settings applied successfully.")
                            
                    except Exception as e:
                        st.error(f"Error loading config: {e}")
            st.markdown("---")
            
            part_options = ["(None)"] + raw_cols
            default_ix = part_options.index('participant') if 'participant' in part_options else 0
            
            part_key = "glob_part_col"
            ensure_choice(part_key, part_options, part_options[default_ix])
            part_col_select = st.selectbox(
                "Participant ID Column", part_options,
                key=part_key,
                help="Used to label results and keep Shift operations within each participant's trials."
            )
            
            rad_key = "glob_group_method"
            group_method = st.radio(
                "Group Data By:", 
                ["File Name (1 file = 1 row)", "Participant ID (Merge files)"],
                key=rad_key,
                help="Determines how data is merged.\n- File Name: Safest. Prevents merging different people with same ID.\n- Participant ID: Merges all files with same ID into a single row (pools trials). Useful for multi-session studies where you want one average per participant."
            )
            
            if group_method.startswith("File Name"): actual_group_col = name_col
            else: actual_group_col = name_col if part_col_select == "(None)" else part_col_select

            # Placeholder, filled after the preprocessing pipeline: warns when
            # rows have no value in the grouping column (every groupby in the
            # analysis engine drops them silently). Created here so the message
            # appears next to the grouping choice it relates to.
            slot_nan_ids = st.empty()

            kc_key = "glob_keep_cols"
            if kc_key not in st.session_state: st.session_state[kc_key] = []
            # Re-assert before render: Streamlit 1.50 resets multiselect state
            # when the options list changes (here: when the participant column
            # changes, since it is excluded from the options). Same fix as the
            # RT/Error grouping selectors.
            _kc_opts = [c for c in raw_cols if c != part_col_select]
            _kc_opts_set = set(_kc_opts)
            st.session_state[kc_key] = [v for v in st.session_state[kc_key] if v in _kc_opts_set]
            keep_cols = st.multiselect(
                "Metadata Columns in Results (optional)",
                _kc_opts,
                key=kc_key,
                help="Copy participant-level metadata (e.g., age or group) into the results CSV. "
                     "Each column uses the first row's value per participant/group. "
                     "All input columns remain available for preprocessing and analysis."
            )
            st.caption("RT, accuracy and condition columns are available automatically. "
                       "Choose them in the analysis tabs. Use this optional field only "
                       "for extra metadata in the results CSV.")

            # --- 2. GLOBAL FILTERS (INPUTS ONLY) ---
            st.markdown("---")
            st.header("2. Global Filters")
            
            # 2.1 Min Rows
            st.markdown("**2.1 Minimum Row Number Filter**")
            mr_key = "glob_use_min_rows"
            if mr_key not in st.session_state: st.session_state[mr_key] = False
            use_min_rows = st.checkbox(
                "Enable filtering by minimum number of rows",
                key=mr_key,
                help="If completed experiments have a specific number of rows, this option can be used to exclude participants who did not finish the experiment."
            )
            
            mrc_key = "glob_min_row_count"
            if mrc_key not in st.session_state: st.session_state[mrc_key] = 100
            min_row_count = st.number_input(
                "Minimum number of rows per file", min_value=1, 
                disabled=not use_min_rows,
                key=mrc_key,
                help="Exclude files with fewer than this many rows. The count includes the header row, matching the last row number shown in Excel. Open a complete participant file in Excel and enter the row number of the last row."
            )
            
            # Apply Min Rows IMMEDIATELY (Safe before shifts)
            df_row_filtered = df_raw  # stlite: immutable raw frame; pipeline uses CoW
            if use_min_rows:
                # +1 adds the header row so the threshold matches the last
                # row number visible in Excel (header = row 1, data starts row 2).
                # stlite: raw file counts are invariant until the upload changes.
                if '_raw_file_counts' not in st.session_state:
                    st.session_state._raw_file_counts = df_raw[name_col].value_counts() + 1
                file_counts = st.session_state._raw_file_counts
                valid_files = file_counts[file_counts >= min_row_count].index
                df_row_filtered = df_row_filtered[df_row_filtered[name_col].isin(valid_files)]
                st.caption(f"Rows remaining: {len(df_row_filtered)}")
                slot_min_excluded = st.empty()
            
            # 2.2 Loops (INPUTS ONLY - Applied later)
            st.markdown("**2.2 Main Loop(s) Filter**")
            loop_candidates = [c for c in raw_cols if 'thisN' in c]
            loop_map = {c.replace('_thisN', '').replace('.thisN', ''): c for c in loop_candidates}

            lp_key = "glob_loops"
            if lp_key not in st.session_state: st.session_state[lp_key] = []
            selected_loop_names = st.multiselect(
                "Filter by Main Loop(s)", sorted(list(loop_map.keys())),
                key=lp_key,
                help="PsychoPy only. Select the loop(s) corresponding to your main experiment. Loops are detected from columns containing `thisN`. Trials where these loops are not active will be excluded."
            )
            if not loop_candidates:
                st.caption("No loop columns detected (requires PsychoPy `thisN` columns).")
            loop_cols_to_filter = [loop_map[name] for name in selected_loop_names]
            
            # Placeholder for count
            slot_loops_count = st.empty()

            # --- 3. SETTINGS ---
            st.markdown("---")
            st.header("3. Reporting")
            st.markdown("**Trial Counts & Limits**")
            
            sc_key = "glob_show_counts"
            if sc_key not in st.session_state: st.session_state[sc_key] = False
            show_counts = st.checkbox(
                "Include trial counts and outlier limits",
                key=sc_key,
                help="If checked, adds columns including Pre-Filter, Rejected by Filter, Outliers, and the calculated Upper/Lower outlier bounds."
            )
            # Store settings for report
            glob_settings = {
                'use_min_rows': use_min_rows, 'min_row_count': min_row_count, 'loop_cols': selected_loop_names, 'keep_cols': keep_cols,
                'show_counts': show_counts
            }

            st.markdown("---")
            st.header("4. Export")
            
            # Fill after the tabs have rendered every setting for this rerun.
            current_config_slot = st.empty()
            st.write("") # Spacer

            st.caption("Bundles the results CSV, settings report and run configuration in one ZIP.")
            # One-shot: the actual run happens in the download logic at the bottom
            # of the script, after the tabs have built the analysis configs.
            run_analysis_clicked = st.button("🚀 Run Analysis & Prepare Downloads", type="primary")
            analysis_issue_slot = st.empty()
        elif not _waiting_for_replacement:
            # Keep navigation available after a failed upload.
            st.error("Could not read files.")
    else:
        if (st.session_state.get('_last_parsed_sig') is not None
                or '_replacement_request' in st.session_state
                or '_current_upload_transition' in st.session_state):
            stage_removed_dataset()
        st.info("👆 Please upload your CSV or ZIP data to begin.")
        if st.session_state.get('_pending_dataset_change', {}).get('changed'):
            st.caption("Your current settings are preserved for the next dataset.")

# stlite: keep the same five navigation tabs before and after an upload.
# Panels that need data explain the next step until a dataset is ready.
st.markdown("""
    <style>
    /* stlite: [role=...] added for Streamlit 1.62's React Aria tabs. */
    /* stlite: enlarged labels need their own line box and vertical space;
       React Aria clips vertical overflow in its horizontally scrolling list. */
    [data-testid="stTabs"] [role="tablist"], div[data-baseweb="tab-list"] {
        padding-top: 0.2rem;
        min-height: 3.2rem;
    }
    [data-testid="stTabs"] [role="tab"], [data-baseweb="tab"] {
        height: auto;
        min-height: 3rem;
        padding-top: 0.3rem;
        padding-bottom: 0.3rem;
    }
    div[data-baseweb="tab-list"] p, div[role="tablist"] p {
        font-size: 1.2rem;
        line-height: 1.5;
        margin: 0;
    }
    div.block-container { padding-top: 1rem !important; }
    /* Cap widget width in tab panels so inputs don't stretch across wide screens */
    div[data-baseweb="tab-panel"] div[data-testid="stSelectbox"],
    div[role="tabpanel"] div[data-testid="stSelectbox"],
    div[data-baseweb="tab-panel"] div[data-testid="stMultiSelect"],
    div[role="tabpanel"] div[data-testid="stMultiSelect"],
    div[data-baseweb="tab-panel"] div[data-testid="stTextInput"],
    div[role="tabpanel"] div[data-testid="stTextInput"],
    div[data-baseweb="tab-panel"] div[data-testid="stTextArea"],
    div[role="tabpanel"] div[data-testid="stTextArea"],
    div[data-baseweb="tab-panel"] div[data-testid="stNumberInput"],
    div[role="tabpanel"] div[data-testid="stNumberInput"],
    div[data-baseweb="tab-panel"] div[data-testid="stRadio"],
    div[role="tabpanel"] div[data-testid="stRadio"] {
        max-width: 680px;
    }
    </style>
""", unsafe_allow_html=True)

# stlite: create navigation once, independently of data or privacy gates.
tab_about, tab_guide, tab_trans, tab_rt, tab_err = st.tabs(
    ["ℹ️ About", "📖 User Guide", "⚙️ Data Preprocessing", "⏱️ Reaction Times", "❌ Error Rates"])
_inject_sticky_tabs()
with tab_about:
    _render_about(runtime_versions.get('Francis'))
with tab_guide:
    _render_guide()

if df_raw is None:
    for _panel, _title in ((tab_trans, "Data Preprocessing"),
                           (tab_rt, "Reaction Time Analysis"),
                           (tab_err, "Error Rate Analysis")):
        with _panel:
            st.subheader(_title)
            st.info("Choose Reset settings or Keep settings in the sidebar to load the new dataset."
                    if _waiting_for_replacement else
                    "Upload a CSV or ZIP file in the sidebar to configure this step.")
    st.stop()

# ==========================================
# 5. DATA PIPELINE: ORDERED PREPROCESSING
# ==========================================

_unary_ops = ["Log (Natural: ln A)", "Log (Base 10: log10 A)", "Square Root (sqrt A)", "Inverse (1 / A)", "Absolute Value (|A|)"]

# stlite: analysis widgets do not invalidate preprocessing.
_pipeline = preprocess_cached(df_row_filtered, loop_cols_to_filter, actual_group_col,
                              name_col, st.session_state.get('_last_parsed_sig', id(df_raw)),
                              raw_frame=df_raw)
df_main_process = _pipeline['frame']
_trial_filter_cache = {}  # expression -> filtered rows, for this rerun only
summary_unit = analysis_unit(actual_group_col, name_col)
preprocessing_report = _pipeline['report']
data_retention = _pipeline['retention']
unavailable_preprocessing = _pipeline['unavailable_inputs']
preprocessing_issues = []
if slot_min_excluded:
    stage = 'Minimum row filter'
    messages = list(exclusion_messages(stage, data_retention['excluded'][stage], limit=20))
    if messages:
        slot_min_excluded.warning('\n\n'.join(messages))
if slot_loops_count:
    with slot_loops_count.container():
        st.caption(f"Rows remaining: {_pipeline['loop_rows']}")
        stage = 'Main loop filter'
        for message in exclusion_messages(stage, data_retention['excluded'][stage], limit=20):
            st.warning(message)
for _warning in _pipeline['warnings']:
    st.warning(_warning)
all_cols = sorted(list(df_main_process.columns))
rt_candidates = [c for c in all_cols if c.endswith('_rt') or c.endswith('.rt')]
corr_candidates = [c for c in all_cols if c.endswith('.corr') or c.endswith('_corr')]

# Rows with no value in the grouping column are silently dropped by every
# groupby in the analysis engine. Surface the count via the sidebar slot —
# a pipeline-stage st.warning in the main area would render above the tabs
# and trigger the tab-flash bug. With File Name grouping the bookkeeping
# column is never NaN, so this only fires for participant-ID grouping.
if slot_nan_ids is not None and actual_group_col in df_main_process.columns:
    _n_nan_ids = int(df_main_process[actual_group_col].isna().sum())
    if _n_nan_ids:
        slot_nan_ids.warning(
            f"{_n_nan_ids} row(s) have no value in '{actual_group_col}' and will be "
            f"excluded from all analyses (rows are grouped by this column)."
        )

# Defined before the tabs because both analysis tabs (RT and Error) consume it.
cols_to_export = []
if part_col_select != "(None)": cols_to_export.append(part_col_select)
cols_to_export.extend(keep_cols)
cols_to_export.append(name_col)
# keep_cols may also contain name_col — dedupe (order-preserving) so the
# output cannot carry the same column twice.
cols_to_export = list(dict.fromkeys(cols_to_export))

# ==========================================
# 6. MAIN INTERFACE (TABS)
# ==========================================
# stlite: navigation and the two reference panels are already rendered above.

# --- TAB 2: DATA PREPROCESSING ---
with tab_trans:
    st.subheader("Data Preprocessing")
    st.markdown(
        "Preprocessing blocks run in the order shown, after Global Filters (sidebar). "
        "Use **New Column** blocks to derive new variables, **Missing Data Removal** to drop "
        "trials with NaN or zero values, and **Extreme Value Rejection** to discard trials "
        "outside an RT range."
    )
    st.caption("💡 Global Filters (minimum row count, loop filter) in the sidebar always run before these blocks.")

    _ops = [
        "Concatenate (A_B)",
        "Ratio (A / B)", "Multiplication (A * B)", "Sum (A + B)", "Difference (A - B)",
        "Log (Natural: ln A)", "Log (Base 10: log10 A)", "Square Root (sqrt A)", "Inverse (1 / A)", "Absolute Value (|A|)"
    ]
    _block_types = [*_NEW_COLUMN_TYPES, "Missing Data Removal", "Extreme Value Rejection"]
    _row_removal_types = {"Missing Data Removal", "Extreme Value Rejection"}

    output_names = {
        bid: output_column_name(st.session_state.get(f'tr_type_{bid}'), st.session_state.get(f'tr_name_{bid}'))
        for bid in st.session_state.trans_blocks
    }
    output_name_counts = Counter(output_names.values())
    n_blocks = len(st.session_state.trans_blocks)
    for idx, block_id in enumerate(st.session_state.trans_blocks):
        with st.expander(f"Preprocessing Block {idx + 1}", expanded=True):

            # ── Move up / down ─────────────────────────────────────────────
            if n_blocks > 1:
                mc1, mc2, _ = st.columns([1, 1, 10])
                with mc1:
                    st.button("▲", key=f"tr_up_{block_id}", disabled=(idx == 0),
                              on_click=move_trans_block_up, args=(block_id,),
                              help="Move block up")
                with mc2:
                    st.button("▼", key=f"tr_dn_{block_id}", disabled=(idx == n_blocks - 1),
                              on_click=move_trans_block_down, args=(block_id,),
                              help="Move block down")

            # ── Type selector ──────────────────────────────────────────────
            type_key = f"tr_type_{block_id}"
            block_type = st.selectbox(
                "Block type", _block_types, key=type_key,
                index=None, placeholder="Select block type…"
            )
            if block_id in unavailable_preprocessing:
                issue = unavailable_column_message(f"Preprocessing block {idx + 1}",
                                                   unavailable_preprocessing[block_id])
                preprocessing_issues.append(issue)
                st.warning(issue + " Restore the columns in an earlier block or change the selection.")
            new_name = output_names[block_id]
            if protected_name := _pipeline['protected_outputs'].get(block_id):
                issue = (f"Preprocessing block {idx + 1} targets the protected identity column "
                         f"{protected_name!r}. Choose a different new column name.")
                preprocessing_issues.append(issue)
                st.warning(issue)
            elif new_name and new_name in raw_cols:
                st.warning(f"⚠️ {new_name!r} already exists in the uploaded data. Choose a different name to avoid overwriting it.")
            elif new_name and output_name_counts[new_name] > 1:
                st.warning(f"⚠️ Another block also creates a column called {new_name!r}. Rename one to avoid a conflict.")

            # ── Block-type description ─────────────────────────────────────
            _descriptions = {
                "New Column — Shift Values": (
                    "Bring the value from a neighbouring trial into the current row. "
                    "A shift of +1 fetches the value from the previous trial; −1 fetches from the next. "
                    "Shift stays within each source file and, when a Participant ID Column is selected, within each participant. "
                    "The new column is immediately available in later blocks and in the analysis tabs."
                ),
                "New Column — Arithmetic": (
                    "Derive a new column from one or two existing columns using arithmetic "
                    "or logarithm operations. The new column is immediately available "
                    "in later blocks and in the analysis tabs."
                ),
                "New Column — Recode Values": (
                    "Create a new column by assigning values based on conditions. Define one or "
                    "more rules — each pairs a filter expression with an output value. Rules are "
                    "evaluated in order and the first matching rule wins. Rows matching no rule "
                    "receive the default value."
                ),
                "Missing Data Removal": (
                    "Remove entire trials (rows) where a selected column contains a missing "
                    "or unwanted value. Choose which values count as missing using the "
                    "checkboxes below."
                ),
                "New Column — Custom Formula": (
                    "Derive a new column using arithmetic, numeric comparisons, or maths functions. "
                    "For example, use `sqrt(col_a)` to take the square root of a column. "
                    "The new column is immediately available in later blocks and in the analysis tabs."
                ),
                "Extreme Value Rejection": (
                    "Remove entire trials (rows) where a reaction time column falls outside "
                    "a defined minimum–maximum range."
                ),
            }
            if block_type in _descriptions:
                st.caption(_descriptions[block_type])

            # ── SHIFT A COLUMN ─────────────────────────────────────────────
            if block_type == "New Column — Shift Values":
                k_name = f"tr_name_{block_id}"
                k_a    = f"tr_cola_{block_id}"
                k_amt  = f"tr_shift_amt_{block_id}"

                if k_name not in st.session_state: st.session_state[k_name] = ""
                if k_a    not in st.session_state: st.session_state[k_a]    = ""
                if k_amt  not in st.session_state: st.session_state[k_amt]  = 1

                opts_a = [""] + all_cols

                st.text_input("New Column Name", key=k_name, placeholder="e.g. prev_task",
                              help="Name for the new column. Avoid spaces.")

                st.selectbox("Column to Shift", retain_column_options(k_a, opts_a), key=k_a,
                             help="The column whose values will be shifted into the new column.")
                st.number_input("Shift Amount (+1 = previous trial, −1 = next trial)", step=1, key=k_amt,
                                help="Positive values look back. Negative values look forward.")
                if missing_ids := _pipeline['shift_missing_ids'].get(block_id):
                    st.warning(f"Shifted values are blank on {missing_ids:,} trial(s) with a missing participant ID.")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

                preceding_types = [
                    st.session_state.get(f"tr_type_{bid}", "")
                    for bid in st.session_state.trans_blocks[:idx]
                ]
                if any(t in _row_removal_types for t in preceding_types):
                    st.warning(
                        "⚠️ A row-removal block precedes this Shift. The shift will be "
                        "computed on already-filtered data, which may alter N-1 trial "
                        "adjacency. Move this block above all Missing Data Removal and "
                        "Extreme Value Rejection blocks to avoid this."
                    )

            # ── CREATE NEW COLUMN ──────────────────────────────────────────
            elif block_type == "New Column — Arithmetic":
                k_name = f"tr_name_{block_id}"
                k_a    = f"tr_cola_{block_id}"
                k_op   = f"tr_op_{block_id}"
                k_b    = f"tr_b_{block_id}"

                if error := _pipeline['arithmetic_errors'].get(block_id):
                    issue = f"Preprocessing block {idx + 1} could not apply Arithmetic: {error}"
                    preprocessing_issues.append(issue)
                    st.warning(issue)

                if k_name not in st.session_state: st.session_state[k_name] = ""
                if k_a    not in st.session_state: st.session_state[k_a]    = ""
                if k_op   not in st.session_state: st.session_state[k_op]   = _ops[0]
                if k_b    not in st.session_state: st.session_state[k_b]    = ""

                opts_a = [""] + all_cols
                ensure_choice(k_op, _ops, _ops[0])
                is_unary = st.session_state[k_op] in _unary_ops

                st.text_input("New Column Name", key=k_name, placeholder="e.g. log_rt",
                              on_change=trim_arithmetic_name, args=(k_name,),
                              help="Francis trims leading and trailing whitespace when you edit this name.")
                if new_name != new_name.strip():
                    st.caption(f"This saved column name includes surrounding whitespace ({new_name!r}).")

                st.selectbox("Column A", retain_column_options(k_a, opts_a), key=k_a,
                             help="The primary column for the operation.")
                st.selectbox("Operator", _ops, key=k_op)

                if not is_unary:
                    opts_b = [""] + all_cols
                    st.selectbox("Column B", retain_column_options(k_b, opts_b), key=k_b,
                                 help="The secondary column for the operation.")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

            # ── CONDITIONAL COLUMN ────────────────────────────────────────
            elif block_type == "New Column — Recode Values":
                k_name    = f"tr_name_{block_id}"
                k_rules   = f"tr_cond_rules_{block_id}"
                k_default = f"tr_cond_default_{block_id}"

                if k_rules not in st.session_state:
                    st.session_state[k_rules] = []

                st.text_input("New Column Name", key=k_name, placeholder="e.g. switch_rep",
                              help="Name for the new column. Avoid spaces.")
                output_type_key = f"tr_cond_output_type_{block_id}"
                ensure_choice(output_type_key, ['Text', 'Number'], 'Text')
                st.selectbox(
                    "Output type", ['Text', 'Number'], key=output_type_key,
                    help="Text keeps labels as entered, including leading zeros. Choose Number for scores or "
                         "calculations. A numeric filter like `score == 1` matches Number output. "
                         "For Text output, use `score == '1'`. In Number mode, blank outputs are missing values."
                )
                if error := _pipeline['recode_errors'].get(block_id):
                    issue = f"Preprocessing block {idx + 1} has an invalid Number output. {error}"
                    preprocessing_issues.append(issue)
                    st.warning(issue)

                st.markdown("**Rules** *(evaluated in order — first match wins)*")
                rule_ids = st.session_state[k_rules]
                if rule_ids:
                    hc1, hc2, hc3 = st.columns([4, 3, 1])
                    hc1.caption("Condition (filter expression)")
                    hc2.caption("Output value")
                for rule_id in rule_ids:
                    k_cond = f"tr_cond_cond_{rule_id}"
                    k_val  = f"tr_cond_val_{rule_id}"
                    if k_cond not in st.session_state:
                        st.session_state[k_cond] = ""
                    if k_val not in st.session_state:
                        st.session_state[k_val] = ""
                    rc1, rc2, rc3 = st.columns([4, 3, 1])
                    with rc1:
                        st.text_input("Condition", key=k_cond,
                                      placeholder="e.g. task == prev_task",
                                      label_visibility="collapsed")
                    with rc2:
                        st.text_input("Value", key=k_val,
                                      placeholder="e.g. repeat",
                                      label_visibility="collapsed")
                    with rc3:
                        st.button("✕", key=f"tr_cond_rm_{rule_id}",
                                  on_click=remove_cond_rule, args=(block_id, rule_id),
                                  help="Remove this rule")

                if not rule_ids:
                    st.caption("No rules yet — add one below.")

                st.button("＋ Add Rule", key=f"tr_cond_add_{block_id}",
                          on_click=add_cond_rule, args=(block_id,))
                st.text_input("Default value (if no rule matches)", key=k_default,
                              placeholder="e.g. switch",
                              help="Value assigned to every row that does not match any rule above.")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

            # ── CUSTOM FORMULA ────────────────────────────────────────────
            elif block_type == "New Column — Custom Formula":
                k_name    = f"tr_name_{block_id}"
                k_formula = f"tr_formula_{block_id}"

                if k_name    not in st.session_state: st.session_state[k_name]    = ""
                if k_formula not in st.session_state: st.session_state[k_formula] = ""

                st.text_input("New Column Name", key=k_name, placeholder="e.g. harmonic_mean",
                              help="Name for the new column. Avoid spaces.")

                st.text_input(
                    "Formula",
                    key=k_formula,
                    placeholder="e.g. (2 * col_a * col_b) / (col_a + col_b)",
                    help=(
                        "Use column names, numbers, arithmetic (+, -, *, /, //, %, **), and numeric comparisons. "
                        f"Supported functions take one argument ({', '.join(_FORMULA_MATH_FUNCTIONS)}). "
                        "PsychoPy column names with dots are converted to underscores "
                        "(e.g. resp.rt → resp_rt)."
                    ),
                )
                st.caption("💡 PsychoPy column names with dots become underscores — `resp.rt` is `resp_rt` here.")

                formula_err = st.session_state.get(f"_formula_err_{block_id}")
                if formula_err:
                    issue = f"Preprocessing block {idx + 1} could not apply Custom Formula: {formula_err}"
                    preprocessing_issues.append(issue)
                    st.warning(issue)
                elif counts := _pipeline['formula_infinities'].get(block_id):
                    st.warning(formula_infinity_message(*counts) + " Infinite values can affect later calculations.")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

            # ── MISSING DATA REMOVAL ───────────────────────────────────────
            elif block_type == "Missing Data Removal":
                fm_key = f"tr_miss_fm_{block_id}"
                if fm_key not in st.session_state:
                    st.session_state[fm_key] = has_rt
                filter_rt = st.checkbox("Show only RT columns", key=fm_key, disabled=not has_rt,
                                        help="If checked, only columns ending in .rt or _rt are shown.")
                m_opts = rt_candidates if (filter_rt and rt_candidates) else all_cols

                mc_key = f"tr_miss_cols_{block_id}"
                st.multiselect(
                    "Columns to check", retain_column_options(mc_key, m_opts, multiple=True), key=mc_key,
                    help="Select one or more columns. A trial is removed if the condition below is met for ANY of them."
                )

                st.markdown("**Remove trials where the column value is:**")

                nan_key = f"tr_miss_nan_{block_id}"
                if nan_key not in st.session_state: st.session_state[nan_key] = True
                st.checkbox("NaN (missing)", key=nan_key,
                            help="Remove trials where the value is absent (NaN).")

                zero_key = f"tr_miss_zero_{block_id}"
                if zero_key not in st.session_state: st.session_state[zero_key] = False
                st.checkbox("Zero (0)", key=zero_key,
                            help="Remove trials where the value is exactly 0. Useful for RT columns where 0 is physically impossible.")

                other_cb_key = f"tr_miss_other_cb_{block_id}"
                if other_cb_key not in st.session_state: st.session_state[other_cb_key] = False
                use_other = st.checkbox("Other value(s)", key=other_cb_key)

                if use_other:
                    other_key = f"tr_miss_other_{block_id}"
                    if other_key not in st.session_state: st.session_state[other_key] = ""
                    st.text_input(
                        "Values to treat as missing (comma-separated)",
                        key=other_key,
                        placeholder="e.g. 999, -1, missing",
                        help=(
                            "Enter values separated by commas, e.g. `999, -1, missing`. "
                            "Numbers are matched numerically; text is matched exactly as written — "
                            "do not add quotes around text values."
                        )
                    )

                count_info = st.session_state.get(f"_pp_count_{block_id}")
                if count_info:
                    n_before, n_after = count_info
                    st.caption(f"{n_before - n_after:,} trial(s) removed · {n_after:,} remaining")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

            # ── EXTREME VALUE REJECTION ────────────────────────────────────
            elif block_type == "Extreme Value Rejection":
                fe_key = f"tr_ext_fe_{block_id}"
                if fe_key not in st.session_state:
                    st.session_state[fe_key] = has_rt
                filter_rt_e = st.checkbox("Show only RT columns", key=fe_key, disabled=not has_rt,
                                          help="If checked, only columns ending in .rt or _rt are shown.")
                ex_opts = rt_candidates if (filter_rt_e and rt_candidates) else all_cols

                ec_key = f"tr_ext_col_{block_id}"
                # index=None: without it the selectbox auto-selects the first
                # column, silently arming the block (with the default 0.15-4.0
                # range) on the next rerun, before the user has chosen anything.
                st.selectbox("RT Column", retain_column_options(ec_key, ex_opts), key=ec_key,
                             index=None, placeholder="Choose option",
                             help="Trials where this column falls outside the Min/Max range will be removed.")

                min_key = f"tr_ext_min_{block_id}"
                max_key = f"tr_ext_max_{block_id}"
                if min_key not in st.session_state: st.session_state[min_key] = 0.15
                if max_key not in st.session_state: st.session_state[max_key] = 4.0
                # Narrow column keeps the number inputs compact (absolute width not possible via CSS)
                c_narrow, _ = st.columns([1, 2])
                c_narrow.number_input("Min RT", step=0.05, key=min_key,
                                      help="Trials faster than this value will be discarded.")
                c_narrow.number_input("Max RT", step=0.1, key=max_key,
                                      help="Trials slower than this value will be discarded.")

                count_info = st.session_state.get(f"_pp_count_{block_id}")
                if count_info:
                    n_before, n_after = count_info
                    st.caption(f"{n_before - n_after:,} trial(s) removed · {n_after:,} remaining")

                if len(st.session_state.trans_blocks) > 1:
                    st.button("🗑️ Delete block", key=f"tr_del_{block_id}",
                              on_click=remove_trans_block, args=(block_id,), type="secondary")

    st.button("(+) Add New Block", on_click=add_trans_block)

    stage = 'Preprocessing blocks'
    for message in exclusion_messages(stage, data_retention['excluded'][stage], limit=20):
        st.warning(message)

    st.markdown("---")
    # Two-step download: serializing the full preprocessed dataset is expensive,
    # so the CSV is built only when the user asks for it rather than on every
    # rerun. The bytes are frozen together with a signature of everything that
    # determines df_main_process;
    # when the pipeline changes, the snapshot is dropped (freeing the memory)
    # and the button reverts from "Download" to "Prepare". Unlike the analysis
    # export there is no drift warning — a stale intermediate dataset has no
    # archival value, so invalidation is the safer behaviour.
    _pp_sig = json.dumps(
        [st.session_state.get("_last_parsed_sig"), use_min_rows, min_row_count,
         selected_loop_names, preprocessing_report, actual_group_col],  # stlite: counts depend on grouping
        default=str, sort_keys=True,
    )
    _pp_snap = st.session_state.get('_preproc_snapshot')
    if _pp_snap and (preprocessing_issues or _pp_snap['signature'] != _pp_sig):
        st.session_state.pop('_preproc_snapshot', None)
        _pp_snap = None

    if _pp_snap is None:
        # Placeholder so the button can be swapped for the download button
        # within the same rerun after a click.
        _prep_slot = st.empty()
        prepare_clicked = _prep_slot.button(
            "⚙️ Prepare Preprocessed Data (.csv)",
            key="prep_preprocessed",
            disabled=bool(preprocessing_issues),
            help="Builds a CSV of the dataset after all global filters and preprocessing "
                 "blocks have been applied. The download button appears when the file is ready."
        )
        if prepare_clicked and not preprocessing_issues:
            _now = datetime.datetime.now()
            _pp_snap = {
                'csv': await run_with_progress(csv_steps(df_main_process),
                                               "cancel_preprocessed", cancel_preprocessed),  # stlite
                'ts': _now.strftime('%Y-%m-%d_%H-%M-%S'),
                'display_time': _now.strftime('%H:%M:%S'),
                'signature': _pp_sig,
            }
            st.session_state._preproc_snapshot = _pp_snap
            _prep_slot.empty()
            st.success("✅ Preprocessed data is ready to download.")  # stlite
            st.set_page_config(page_title="✅ Data ready · Francis")

    if _pp_snap:
        st.download_button(
            "⬇️ Download Preprocessed Data (.csv)",
            _pp_snap['csv'],
            f"francis_preprocessed_data_{_pp_snap['ts']}.csv",
            "text/csv",
            key="dl_preprocessed", on_click="ignore",  # stlite
            help="Downloads the dataset after all global filters and preprocessing blocks have been applied."
        )
        st.caption(f"Prepared at {_pp_snap['display_time']}")

# --- TAB 2: RT ANALYSIS ---
with tab_rt:
    st.subheader("Reaction Time Analysis")
    rt_preview_configs = []
    rt_name_tracker = {}
    rt_issues = []

    for idx, block_id in enumerate(st.session_state.rt_blocks):
        with st.expander(f"Analysis Block {idx + 1}", expanded=True):

            # === Define Conditions ===
            st.markdown('##### Define Conditions')
            grp_key = f"rt_grp_{block_id}"
            group_cols = st.multiselect(
                "Select columns to split the summary measure into separate conditions",
                retain_column_options(grp_key, ["(None)"] + all_cols, multiple=True),
                key=grp_key,
                help="Select '(None)' to compute a single summary across all data, without splitting by condition."
            )
            include_col_names = st.checkbox(
                "Include column names in output",
                key=f"rt_inc_col_{block_id}",
                help="Useful when condition values are numbers. E.g., 'accuracy0' instead of just '0'."
            )

            # === Select Dependent Variable ===
            st.markdown('---')
            st.markdown('##### Select Dependent Variable')
            _rt_filt_key = f"rt_filt_{block_id}"
            if _rt_filt_key not in st.session_state:
                st.session_state[_rt_filt_key] = (len(rt_candidates) > 0)
            filter_dv = st.checkbox(
                "Show only RT columns (`.rt` / `_rt`)",
                key=_rt_filt_key,
                disabled=not rt_candidates
            )
            dv_opts = rt_candidates if (filter_dv and rt_candidates) else all_cols
            dv_key = f"rt_dv_{block_id}"
            dv_col = st.selectbox("Select column with the dependent variable",
                                  retain_column_options(dv_key, dv_opts), key=dv_key,
                                  index=None, placeholder="Choose option")

            # === Exclude Trials from Analysis ===
            st.markdown('---')
            st.markdown('##### Exclude Trials from Analysis')
            st.markdown(
                '<p style="font-size:0.875rem;color:#333;max-width:680px;margin:0 0 0.4rem 0;">'
                'Use trial filter and outlier rejection to remove specific trials from the analysis. '
                'The trial filter will always run before the outlier rejection. '
                'Use “Condition Name Suffix” to add a short label to the output column names '
                '(e.g. <code>_no_error_trials</code>).</p>',
                unsafe_allow_html=True
            )
            st.markdown('###### Trial Filter')
            extra_filt = st.text_area(
                "Use Pandas query syntax to filter trials",
                key=f"rt_filt_txt_{block_id}",
                placeholder="(Optional)",
                height=80,
                help="Apply a pandas query filter before calculating the summary measure. Examples: `key_resp_corr == 1` or `trials_thisN >= 4`."
            )
            extra_filt = extra_filt.strip().replace('\n', ' ')
            if (extra_filt and not preprocessing_issues and group_cols
                    and all(c in df_main_process.columns for c in group_cols if c != "(None)")):
                _summary = filter_summary(df_main_process, extra_filt, _trial_filter_cache,
                                          actual_group_col, group_cols, summary_unit)
                if _summary:
                    st.caption(_summary)
            st.markdown('###### Outlier Rejection')
            _rt_out_key = f"rt_out_en_{block_id}"
            if _rt_out_key not in st.session_state:
                st.session_state[_rt_out_key] = False
            do_outlier = st.checkbox("Enable outlier rejection", key=_rt_out_key)
            outlier_scope, outlier_method, outlier_thresh = "Per Condition", "Standard Deviation (SD)", 2.5

            if do_outlier:
                with st.container(border=True):
                    osc_key = f"rt_out_sc_{block_id}"
                    outlier_scope = st.radio(
                        "Scope", ["Per Condition", "Global (Participant Level)"], key=osc_key,
                        help="Per Condition: outliers are identified within each condition separately.\nGlobal: outliers are identified across all of the participant's valid trials for this dependent variable."
                    )
                    om_key = f"rt_out_mt_{block_id}"
                    outlier_method = st.selectbox("Method", ["Standard Deviation (SD)", "Median Absolute Deviation (MAD)", "Double MAD", "Percentage Trimming"], key=om_key)
                    # The deviation-multiplier methods (SD/MAD/Double MAD) share
                    # one key — same unit, same bounds. Percentage Trimming uses
                    # its own key: its threshold is a percent (1–49.9), and
                    # sharing the key crashed when a stored multiplier violated
                    # the percent bounds (e.g. SD 0.5 → switch to Trimming,
                    # min 1.0). Separate keys also mean each unit family keeps
                    # its own setting across method switches.
                    ot_key = f"rt_out_th_{block_id}"
                    if ot_key not in st.session_state:
                        st.session_state[ot_key] = 2.5
                    if outlier_method == "Standard Deviation (SD)":
                        outlier_thresh = st.number_input("Threshold (SDs)", min_value=0.1, step=0.5, key=ot_key)
                    elif outlier_method in ["Median Absolute Deviation (MAD)", "Double MAD"]:
                        outlier_thresh = st.number_input("Threshold (MADs)", min_value=0.1, step=0.5, key=ot_key)
                    elif outlier_method == "Percentage Trimming":
                        trim_key = f"rt_out_trim_{block_id}"
                        if trim_key not in st.session_state:
                            st.session_state[trim_key] = 20.0
                        outlier_thresh = st.number_input("Percent to Trim (each end)", min_value=1.0, max_value=49.9, step=0.5, key=trim_key)
                    if outlier_scope.startswith("Global"):
                        if extra_filt.strip():
                            st.info(f"ℹ️ Global bounds will be calculated from trials matching the Trial Filter (`{extra_filt.strip()}`), not from all of the participant's valid trials.")
                        else:
                            st.info("ℹ️ Global bounds will be calculated from all of the participant's valid trials for this dependent variable.")

            st.markdown('###### Condition Name Suffix')
            suffix_val = st.text_input(
                "Append a short label to all output column names for this analysis block (e.g. `_no_errors`).",
                key=f"rt_suf_{block_id}",
                placeholder="(Optional)"
            )

            # === Summary Measure ===
            st.markdown('---')
            st.markdown('##### Summary Measure')
            meas_key = f"rt_meas_{block_id}"
            measure = st.selectbox(
                "What summary measure should be computed on the values in the DV?",
                ["Mean", "Median", "Sum", "Count"],
                key=meas_key
            )
            if measure in ("Mean", "Median"):
                _rt_z_key = f"rt_excl_zero_{block_id}"
                if _rt_z_key not in st.session_state:
                    st.session_state[_rt_z_key] = True
                exclude_zero_dv = st.checkbox(
                    "Exclude trials where the DV is 0",
                    key=_rt_z_key,
                    help="Runs before outlier rejection. Keep this ticked for raw RT columns, "
                         "where an RT of 0 indicates a recording artifact. Untick it if 0 is a "
                         "meaningful value for your dependent variable (e.g. difference scores, "
                         "or log values — ln of an RT of exactly 1 s is 0). Trials with NaN are "
                         "always excluded; negative values are always included."
                )
                _rt_ms_key = f"rt_ms_{block_id}"
                if _rt_ms_key not in st.session_state:
                    st.session_state[_rt_ms_key] = False
                convert_ms = st.checkbox("Convert s to ms", key=_rt_ms_key)
            else:
                exclude_zero_dv = True
                convert_ms = False
            rt_min_tr_key = f"rt_min_tr_{block_id}"
            if rt_min_tr_key not in st.session_state:
                st.session_state[rt_min_tr_key] = 1
            block_min_trials_rt = st.number_input(
                "Minimum trials per condition",
                min_value=1,
                key=rt_min_tr_key,
                help="If the number of valid trials (after filtering and outlier rejection) in a condition falls below this value, the result is set to NaN."
            )

            if len(st.session_state.rt_blocks) > 1:
                st.button("🗑️ Delete Block", key=f"rt_del_{block_id}", on_click=remove_rt_block, args=(block_id,), type="secondary")

            # === Conditions Preview ===
            block_label = f"Reaction Time block {idx + 1}"
            missing = missing_analysis_columns(group_cols, dv_col, all_cols)
            if missing:
                issue = unavailable_column_message(block_label, missing)
                rt_issues.append(issue)
                st.warning(issue + " Restore the columns in preprocessing or change the selection.")
            elif group_cols or dv_col:
                if not dv_col or not group_cols:
                    selection = "a dependent variable" if not dv_col else "grouping columns or (None)"
                    issue = f"{block_label} is incomplete. Select {selection}."
                    rt_issues.append(issue)
                    st.warning(issue)
                elif not preprocessing_issues and filter_matches_no_trials(df_main_process, extra_filt, _trial_filter_cache):
                    issue = no_trials_issue(block_label, extra_filt)
                    rt_issues.append(issue)
                    st.warning(issue)
                elif not preprocessing_issues:
                    # Build this block's configs atomically: collect into a local
                    # list and commit to the shared list only on success, so a
                    # mid-build failure can never leave a partial block in the
                    # export. The name-dedup tracker is restored on failure for
                    # the same reason.
                    block_configs = []
                    _tracker_backup = dict(rt_name_tracker)
                    try:
                        clean_dv = clean_col_name(dv_col)
                        nan_cols_to_show = []

                        if "(None)" in group_cols:
                            base_name = f"{clean_dv}_{measure.lower()}"
                            if suffix_val:
                                base_name += suffix_val
                            final_name = f"{base_name}_{rt_name_tracker.get(base_name, 0) + 1}" if base_name in rt_name_tracker else base_name
                            if base_name in rt_name_tracker:
                                rt_name_tracker[base_name] += 1
                            else:
                                rt_name_tracker[base_name] = 1
                            block_configs.append({
                                "Condition_Name": final_name,
                                "Filter_Logic": extra_filt,
                                "Dependent_Var": dv_col, "Measure": measure,
                                "Factor_Logic": "", "Extra_Logic": extra_filt,
                                "convert_to_ms": convert_ms, "exclude_zero_dv": exclude_zero_dv,
                                "enable_outliers": do_outlier,
                                "outlier_scope": outlier_scope, "outlier_method": outlier_method, "outlier_thresh": outlier_thresh,
                                "min_trials": block_min_trials_rt, "suffix": suffix_val,
                                "block_num": idx + 1, "group_cols": [], "row_values": {}
                            })
                        else:
                            # Apply filter first so the condition list reflects the filtered data.
                            # Gate on _check_expr_safety so a dunder/import expression can't reach
                            # df.query here (the analysis stage guards the same filter via
                            # safe_query). Stay silent on failure — this preview re-runs on every
                            # keystroke, so emitting a warning would spam and risk the tab flash.
                            _kept = apply_trial_filter(df_main_process, extra_filt, _trial_filter_cache)
                            filtered_df = df_main_process if _kept is None else _kept

                            combs = sort_combs(filtered_df[group_cols].drop_duplicates(), group_cols)
                            nan_mask = combs.isnull().any(axis=1)
                            nan_combs = combs[nan_mask]
                            combs = combs[~nan_mask]

                            # Refuse before the per-condition work, not after.
                            if len(combs) > MAX_CONDITIONS_PER_BLOCK:
                                raise _TooManyConditions(len(combs))

                            for col in group_cols:
                                if not nan_combs.empty and nan_combs[col].isnull().any():
                                    nan_cols_to_show.append(col)

                            for _, row in combs.iterrows():
                                name_parts = [format_col_val_label(c, row[c]) if include_col_names else format_val_for_name(row[c]) for c in group_cols]
                                base_name = f"{'_'.join(name_parts)}_{clean_dv}_{measure.lower()}"
                                if suffix_val:
                                    base_name += suffix_val
                                final_name = f"{base_name}_{rt_name_tracker.get(base_name, 0) + 1}" if base_name in rt_name_tracker else base_name
                                if base_name in rt_name_tracker:
                                    rt_name_tracker[base_name] += 1
                                else:
                                    rt_name_tracker[base_name] = 1
                                # Backtick the column (PsychoPy enforces identifier-safe
                                # names, but other tools' output may not be — e.g. a
                                # column named 2back) and escape the value properly.
                                parts = [f"`{col}` == {fmt_query_value(row[col])}" for col in group_cols]
                                base_logic = " and ".join(parts)
                                final_logic = f"({base_logic}) and ({extra_filt})" if extra_filt.strip() else base_logic
                                block_configs.append({
                                    "Condition_Name": final_name, "Filter_Logic": final_logic,
                                    "Dependent_Var": dv_col, "Measure": measure,
                                    "Factor_Logic": base_logic, "Extra_Logic": extra_filt,
                                    "convert_to_ms": convert_ms, "exclude_zero_dv": exclude_zero_dv,
                                    "enable_outliers": do_outlier,
                                    "outlier_scope": outlier_scope, "outlier_method": outlier_method, "outlier_thresh": outlier_thresh,
                                    "min_trials": block_min_trials_rt, "suffix": suffix_val,
                                    "block_num": idx + 1, "group_cols": group_cols,
                                    "row_values": {col: row[col] for col in group_cols}
                                })

                        rt_preview_configs.extend(block_configs)
                        if block_configs:
                            preview_df = pd.DataFrame(block_configs)[["Condition_Name", "Filter_Logic", "Dependent_Var"]]
                            st.dataframe(
                                preview_df.replace({"Filter_Logic": {"": "All trials"}}),
                                width="stretch", hide_index=True
                            )
                        else:
                            st.caption("No conditions to preview — no value combinations remain (check the trial filter and missing values).")
                        if nan_cols_to_show:
                            nan_labels = [f"`{col} == nan`" for col in nan_cols_to_show]
                            st.caption(f"Conditions not included (column contains missing values): {', '.join(nan_labels)}")
                    except Exception as error:
                        rt_name_tracker.clear()
                        rt_name_tracker.update(_tracker_backup)
                        issue = condition_build_issue(block_label, error)
                        rt_issues.append(issue)
                        st.warning(issue)

    st.button(" (+) Add New Analysis Block", on_click=add_rt_block)
    st.markdown("---")
    st.caption("Click 'Preview Results' to display the result below. Use 'Run Analysis & Prepare Downloads' in the sidebar to prepare the analysis ZIP.")
    rt_preview_blocked = bool(preprocessing_issues or rt_issues)
    rt_preview_clicked = st.button("▶ Preview Results", key="rt_preview_btn", disabled=rt_preview_blocked)
    if rt_preview_clicked and not rt_preview_blocked and rt_preview_configs:
        # stlite: same fast/fallback calculation with Paco-style Cancel.
        res_df = await run_with_progress(analyze_data_blocks_steps(
            df_main_process, rt_preview_configs, actual_group_col, cols_to_export,
            glob_settings, mode="RT", name_col=name_col), "cancel_rt_preview", cancel_analysis,
            busy_message="Preparing reaction-time preview…")
        st.dataframe(organize_final_columns(res_df, cols_to_export), width="stretch")
        _funnel = st.session_state.get("_trial_funnel_RT")
        if _funnel:
            st.caption(funnel_summary(_funnel, "RT", summary_unit, glob_settings['show_counts']))

# --- TAB 3: ERROR ANALYSIS ---
with tab_err:
    st.subheader("Error Rate Analysis")
    err_preview_configs = []
    err_name_tracker = {}
    err_issues = []

    for idx, block_id in enumerate(st.session_state.err_blocks):
        with st.expander(f"Analysis Block {idx + 1}", expanded=True):

            # === Define Conditions ===
            st.markdown('##### Define Conditions')
            grp_key = f"err_grp_{block_id}"
            groups = st.multiselect(
                "Select columns to split the error rate into separate conditions",
                retain_column_options(grp_key, ["(None)"] + all_cols, multiple=True),
                key=grp_key,
                help="Select '(None)' to compute a single error rate across all data, without splitting by condition."
            )
            include_col_names = st.checkbox(
                "Include column names in output",
                key=f"err_inc_{block_id}",
                help="Useful when condition values are numbers. E.g., 'block1' instead of just '1' when analysing accuracies for each block."
            )

            # === Select Target Column ===
            st.markdown('---')
            st.markdown('##### Select Target Column')
            _err_filt_key = f"err_filt_{block_id}"
            if _err_filt_key not in st.session_state:
                st.session_state[_err_filt_key] = (len(corr_candidates) > 0)
            filter_only_corr = st.checkbox(
                "Show only accuracy columns (`.corr` / `_corr`)",
                key=_err_filt_key,
                disabled=not corr_candidates
            )
            tgt_opts = corr_candidates if (filter_only_corr and corr_candidates) else all_cols
            tc_key = f"err_col_{block_id}"
            target_col = st.selectbox(
                "Select column with accuracy values",
                retain_column_options(tc_key, tgt_opts),
                key=tc_key,
                index=None,
                placeholder="Choose option"
            )

            # === Define Ratio ===
            st.markdown('---')
            st.markdown('##### Define Ratio')
            st.markdown(
                '<p style="font-size:0.875rem;color:#333;max-width:680px;margin:0 0 0.4rem 0;">'
                'Specify which values in the target column contribute to the numerator '
                'and which values contribute to the denominator. '
                'Separate multiple values with commas (e.g. <code>0, 1</code>). '
                'When using PsychoPy, to calculate error rates, use <code>0</code> as numerator and '
                '<code>0, 1</code> as denominator. '
                'To calculate accuracy, use <code>1</code> as numerator and '
                '<code>0, 1</code> as denominator. </p>',
                unsafe_allow_html=True
            )
            # init-then-render (no value=): these keys are restored by
            # apply_config, and combining a widget default with a
            # config-written key is the conflict the handover rule forbids.
            _num_key = f"err_num_{block_id}"
            if _num_key not in st.session_state:
                st.session_state[_num_key] = "0"
            num_val_str = st.text_input("Numerator values", key=_num_key)
            _den_key = f"err_den_{block_id}"
            if _den_key not in st.session_state:
                st.session_state[_den_key] = "0, 1"
            den_val_str = st.text_input("Denominator values", key=_den_key)
            excl_to_key = f"err_excl_to_{block_id}"
            exclude_no_response = st.checkbox(
                "Exclude trials with no response",
                key=excl_to_key,
                help="In PsychoPy, trials where no response was recorded receive a score of 0 in the accuracy "
                     "column, making them indistinguishable from incorrect responses. Ticking this option identifies "
                     "no-response trials via the paired RT column (e.g., `key_resp_rt` for `key_resp_corr`) and "
                     "excludes them from both the numerator and denominator. This option relies on PsychoPy's naming "
                     "convention for response component columns (i.e., `.rt` and `.corr`)."
            )
            expected_rt_col = derive_rt_col(target_col) if exclude_no_response else ''
            available_rt_col = expected_rt_col if expected_rt_col in all_cols else ''
            if exclude_no_response:
                if target_col is None:
                    st.caption("Select a target column first.")
                elif available_rt_col:
                    st.caption(f"Detected RT column: `{available_rt_col}`")
                else:
                    expected = expected_rt_col or '(could not derive name)'
                    st.warning(
                        f"Could not find the expected RT column `{expected}` in the data. "
                        f"Trials with no response will not be excluded."
                    )
            no_response_settings = {
                'rt_col_for_timeouts': available_rt_col,
                'no_response_requested': exclude_no_response,
                'expected_rt_col': expected_rt_col,
            }

            # === Exclude Trials from Analysis ===
            st.markdown('---')
            st.markdown('##### Exclude Trials from Analysis')
            extra_filt_err = st.text_area(
                "Use Pandas query syntax to filter trials",
                key=f"err_ex_{block_id}",
                placeholder="(Optional)",
                height=80,
                help="Apply a pandas query filter before calculating the error rate. Examples: `block in [1, 2]` or `trials_thisN >= 4`."
            )
            extra_filt_err = extra_filt_err.strip().replace('\n', ' ')
            if (extra_filt_err and not preprocessing_issues and groups
                    and all(c in df_main_process.columns for c in groups if c != "(None)")):
                _summary = filter_summary(df_main_process, extra_filt_err, _trial_filter_cache,
                                          actual_group_col, groups, summary_unit, available_rt_col)
                if _summary:
                    st.caption(_summary)

            # === Output ===
            st.markdown('---')
            st.markdown('##### Output')
            suffix_val = st.text_input(
                "Append a short label to all output column names for this analysis block.",
                key=f"err_suf_{block_id}",
                placeholder="(Optional)"
            )
            _err_pct_key = f"err_pct_{block_id}"
            if _err_pct_key not in st.session_state:
                st.session_state[_err_pct_key] = True
            scale_pct = st.checkbox("Convert to %", key=_err_pct_key)
            err_min_tr_key = f"err_min_tr_{block_id}"
            if err_min_tr_key not in st.session_state:
                st.session_state[err_min_tr_key] = 1
            block_min_trials_err = st.number_input(
                "Minimum trials per condition",
                min_value=1,
                key=err_min_tr_key,
                help="If the number of trials in the denominator falls below this value, the result is set to NaN."
            )

            if len(st.session_state.err_blocks) > 1:
                st.button("🗑️ Delete Block", key=f"err_del_{block_id}", on_click=remove_err_block, args=(block_id,), type="secondary")

            # === Conditions Preview ===
            block_label = f"Error Rate block {idx + 1}"
            missing = missing_analysis_columns(groups, target_col, all_cols)
            if missing:
                issue = unavailable_column_message(block_label, missing)
                err_issues.append(issue)
                st.warning(issue + " Restore the columns in preprocessing or change the selection.")
            elif bool(groups) != bool(target_col):
                selection = "an accuracy column" if not target_col else "grouping columns or (None)"
                issue = f"{block_label} is incomplete. Select {selection}."
                err_issues.append(issue)
                st.warning(issue)
            elif (groups and target_col and not preprocessing_issues
                  and filter_matches_no_trials(df_main_process, extra_filt_err, _trial_filter_cache)):
                issue = no_trials_issue(block_label, extra_filt_err)
                err_issues.append(issue)
                st.warning(issue)
            elif groups and target_col and not preprocessing_issues:
                # Use the same atomic condition build as the RT tab.
                block_configs = []
                _tracker_backup = dict(err_name_tracker)
                try:
                    num_vals, den_vals = parse_values_string(num_val_str), parse_values_string(den_val_str)
                    to_ls = lambda v: "[" + ", ".join(fmt_query_value(x) for x in v) + "]"

                    denom_logic_only = f"`{target_col}` in {to_ls(den_vals)}"
                    num_logic = f"`{target_col}` in {to_ls(num_vals)}"
                    clean_tgt = clean_col_name(target_col)
                    nan_cols_to_show = []

                    if "(None)" not in groups:
                        # Apply the trial filter first so the condition list reflects
                        # the filtered data, as the RT tab does. Otherwise a value the
                        # filter excludes (e.g. cond != 'none') still becomes a
                        # condition and yields an all-NaN output column. Same safety
                        # gate and silent fallback as the RT preview.
                        _kept = apply_trial_filter(df_main_process, extra_filt_err, _trial_filter_cache)
                        filtered_df = df_main_process if _kept is None else _kept
                        combs = sort_combs(filtered_df[groups].drop_duplicates(), groups)
                        # Drop condition combinations with a missing grouping value, as
                        # the RT tab does. Otherwise a `col == nan` query is generated
                        # for each NaN combo, which raises (or matches nothing) — one
                        # st.warning per participant plus a spurious all-NaN column
                        # named with "nan". The affected columns are surfaced via the
                        # caption below instead.
                        nan_mask = combs.isnull().any(axis=1)
                        nan_combs = combs[nan_mask]
                        combs = combs[~nan_mask]
                        for col in groups:
                            if not nan_combs.empty and nan_combs[col].isnull().any():
                                nan_cols_to_show.append(col)
                        # Refuse before the per-condition work, not after.
                        if len(combs) > MAX_CONDITIONS_PER_BLOCK:
                            raise _TooManyConditions(len(combs))
                        for _, row in combs.iterrows():
                            if include_col_names:
                                name_parts = [format_col_val_label(c, row[c]) for c in groups]
                            else:
                                name_parts = [format_val_for_name(row[c]) for c in groups]

                            base_name = f"{'_'.join(name_parts)}_{clean_tgt}{suffix_val}"
                            final_name = f"{base_name}_{err_name_tracker.get(base_name, 0) + 1}" if base_name in err_name_tracker else base_name
                            if base_name in err_name_tracker:
                                err_name_tracker[base_name] += 1
                            else:
                                err_name_tracker[base_name] = 1

                            # Same backtick + escape treatment as the RT tab.
                            parts = [f"`{col}` == {fmt_query_value(row[col])}" for col in groups]
                            group_logic = " and ".join(parts)

                            full_denom_calc = f"({group_logic}) and ({denom_logic_only})"
                            if extra_filt_err.strip():
                                full_denom_calc = f"({full_denom_calc}) and ({extra_filt_err})"

                            block_configs.append({
                                "Condition_Name": final_name, "Target_Column": target_col,
                                "Filter_Logic_Denominator": full_denom_calc,
                                "Numerator_Logic": num_logic, "Numerator_Values": num_val_str,
                                "Denominator_Logic": denom_logic_only,
                                "Denominator_Values": den_val_str,
                                "Group_Logic": group_logic,
                                "Extra_Logic": extra_filt_err,
                                "Measure": "Percentage" if scale_pct else "Ratio", "Scale_To_Pct": scale_pct,
                                **no_response_settings,
                                "min_trials": block_min_trials_err, "suffix": suffix_val,
                                "block_num": idx + 1, "group_cols": groups,
                                "row_values": {col: row[col] for col in groups}
                            })
                    else:
                        base_name = f"{clean_tgt}{suffix_val}"
                        final_name = f"{base_name}_{err_name_tracker.get(base_name, 0) + 1}" if base_name in err_name_tracker else base_name
                        if base_name in err_name_tracker:
                            err_name_tracker[base_name] += 1
                        else:
                            err_name_tracker[base_name] = 1

                        full_denom_calc = denom_logic_only
                        if extra_filt_err.strip():
                            full_denom_calc = f"({full_denom_calc}) and ({extra_filt_err})"

                        block_configs.append({
                            "Condition_Name": final_name, "Target_Column": target_col,
                            "Filter_Logic_Denominator": full_denom_calc,
                            "Numerator_Logic": num_logic, "Numerator_Values": num_val_str,
                            "Denominator_Logic": denom_logic_only,
                            "Denominator_Values": den_val_str,
                            "Group_Logic": "All Data",
                            "Extra_Logic": extra_filt_err,
                            "Measure": "Percentage" if scale_pct else "Ratio", "Scale_To_Pct": scale_pct,
                            **no_response_settings,
                            "min_trials": block_min_trials_err, "suffix": suffix_val,
                            "block_num": idx + 1, "group_cols": [], "row_values": {}
                        })

                    err_preview_configs.extend(block_configs)
                    if block_configs:
                        preview_df = pd.DataFrame(block_configs)[["Condition_Name", "Filter_Logic_Denominator", "Numerator_Logic", "Denominator_Logic", "Measure"]]
                        preview_df = preview_df.rename(columns={
                            "Filter_Logic_Denominator": "Trial Selection",
                            "Numerator_Logic": "Error Trials",
                            "Denominator_Logic": "All Valid Trials",
                        })
                        st.dataframe(preview_df, width="stretch", hide_index=True)
                    else:
                        st.caption("No conditions to preview — no value combinations remain.")
                    if nan_cols_to_show:
                        nan_labels = [f"`{col} == nan`" for col in nan_cols_to_show]
                        st.caption(f"Conditions not included (column contains missing values): {', '.join(nan_labels)}")
                except Exception as error:
                    err_name_tracker.clear()
                    err_name_tracker.update(_tracker_backup)
                    issue = condition_build_issue(block_label, error)
                    err_issues.append(issue)
                    st.warning(issue)

    st.button(" (+) Add New Analysis Block", key="err_add_block", on_click=add_err_block)
    st.markdown("---")
    st.caption("Click 'Preview Results' to display the result below. Use 'Run Analysis & Prepare Downloads' in the sidebar to prepare the analysis ZIP.")
    err_preview_blocked = bool(preprocessing_issues or err_issues)
    err_preview_clicked = st.button("▶ Preview Results", key="err_preview_btn", disabled=err_preview_blocked)
    if err_preview_clicked and not err_preview_blocked and err_preview_configs:
        # stlite: same fast/fallback calculation with Paco-style Cancel.
        res_df = await run_with_progress(analyze_data_blocks_steps(
            df_main_process, err_preview_configs, actual_group_col, cols_to_export,
            glob_settings, mode="Error", name_col=name_col), "cancel_err_preview", cancel_analysis,
            busy_message="Preparing error-rate preview…")
        st.dataframe(organize_final_columns(res_df, cols_to_export), width="stretch")
        _funnel = st.session_state.get("_trial_funnel_Error")
        if _funnel:
            st.caption(funnel_summary(_funnel, "Error", summary_unit, glob_settings['show_counts']))

# --- DOWNLOAD LOGIC ---
# Snapshot semantics: clicking "Run Analysis & Prepare Downloads" computes the
# analysis once and freezes a completed ZIP and its signature together. The ZIP
# and its filename stay tied to the run even when live settings change.

# Capture widget values after all tabs render, including defaults set this rerun.
current_config_json = json.dumps(get_config_dict(), indent=2, sort_keys=True)
current_config_slot.download_button(
    "💾 Save Current Configuration (.json)",
    data=current_config_json,
    file_name=f"francis_config_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json",
    mime="application/json", on_click="ignore",  # stlite
    help="Saves your current settings. The analysis ZIP contains the configuration paired with its results."
)

# Everything that determines the export output, serialized for the drift check.
# All inputs are small and rebuilt on every rerun anyway, so this is cheap.
_live_sig = json.dumps(
    [st.session_state.get("_last_parsed_sig"), glob_settings, group_method,
     part_col_select, preprocessing_report, rt_preview_configs, err_preview_configs, current_config_json],
    default=str, sort_keys=True,
)

export_issues = preprocessing_issues + rt_issues + err_issues
if export_issues:
    analysis_issue_slot.warning(
        "Results cannot be prepared until these block warnings are resolved.\n\n"
        + "\n".join(f"- {issue}" for issue in export_issues)
    )

if run_analysis_clicked and not export_issues:
    # stlite: one busy status spans RT, errors and export, with no phase resets.
    _analysis_slot = st.sidebar.empty()
    _analysis_status = DelayedBusyStatus(
        _analysis_slot, 'cancel_analysis', cancel_analysis, 'Preparing results…')
    try:
        _t_analysis = time.perf_counter()  # stlite: diagnostics
        df_rt_res = await await_steps(analyze_data_blocks_steps(
            df_main_process, rt_preview_configs, actual_group_col, cols_to_export,
            glob_settings, mode="RT", name_col=name_col), _analysis_status) if rt_preview_configs else pd.DataFrame()  # stlite
        df_err_res = await await_steps(analyze_data_blocks_steps(
            df_main_process, err_preview_configs, actual_group_col, cols_to_export,
            glob_settings, mode="Error", name_col=name_col), _analysis_status) if err_preview_configs else pd.DataFrame()  # stlite

        if df_rt_res.empty and df_err_res.empty:
            st.sidebar.error("No analysis configured!")
            # A failed run must not leave stale files on offer.
            st.session_state.pop('_export_snapshot', None)
        else:
            if df_rt_res.empty: final_df = df_err_res
            elif df_err_res.empty: final_df = df_rt_res
            else:
                common = list(set(df_rt_res.columns) & set(df_err_res.columns))
                merge_on = [actual_group_col] + [c for c in cols_to_export if c in common]
                glob_counts = ["trls_total", "trls_missing", "trls_extreme_global", "trls_valid_global"]
                for gc in glob_counts:
                    if gc in common: merge_on.append(gc)
                merge_on = list(dict.fromkeys(merge_on))
                final_df = pd.merge(df_rt_res, df_err_res, on=merge_on, how='outer')

            final_df = organize_final_columns(final_df, cols_to_export)
            _now = datetime.datetime.now()
            _ts = _now.strftime("%Y-%m-%d_%H-%M-%S")
            folder = f'francis_analysis_{_ts}'
            report = generate_settings_report(
                {**glob_settings, 'timestamp': _ts, 'data_retention': data_retention,
                 'load_messages': _load_msgs, 'upload_signature': st.session_state.get('_last_parsed_sig'),
                 'row_counts': (len(df_raw), len(df_main_process), len(final_df)),
                 'runtime_versions': runtime_versions},
                rt_preview_configs, err_preview_configs, group_method, part_col_select, preprocessing_report
            )
            st.session_state._export_snapshot = {
                'zip': await await_steps(analysis_zip_steps(final_df, report, current_config_json, folder, _now),
                                         _analysis_status),
                'filename': f'{folder}.zip',
                'display_time': _now.strftime("%H:%M:%S"),
                'signature': _live_sig,
            }
        st.session_state._diag_analysis = time.perf_counter() - _t_analysis  # stlite
        if not (df_rt_res.empty and df_err_res.empty):
            st.sidebar.success("✅ Analysis ZIP is ready to download.")
            st.set_page_config(page_title="✅ Results ready · Francis")
    finally:
        _analysis_slot.empty()

_snap = st.session_state.get('_export_snapshot')
if _snap:
    st.sidebar.download_button("⬇️ Download Analysis (.zip)", _snap['zip'], _snap['filename'],
                               "application/zip", key='dl_analysis', on_click='ignore')
    st.sidebar.caption(f"Last generated at {_snap['display_time']}")
    if export_issues:
        st.sidebar.warning("This ZIP is from the last successful run. Resolve the block warnings to prepare new results.")
    elif _snap['signature'] != _live_sig:
        st.sidebar.warning(
            "⚠️ Settings or data have changed since this ZIP was generated. "
            "Click **Run Analysis & Prepare Downloads** to refresh them."
        )

# Timing and memory diagnostics for troubleshooting.
with st.sidebar.expander("Diagnostics"):
    _lines = []
    if "_diag_parse" in st.session_state:
        _s, _mb = st.session_state._diag_parse
        _lines.append(f"- Upload ({_mb:.1f} MB) parsed in **{_s:.1f} s**")
    if df_raw is not None:
        _lines.append(f"- Loaded data: {len(df_raw):,} rows x {df_raw.shape[1]} columns")
    if "_diag_analysis" in st.session_state:
        _lines.append(f"- Last Run Analysis: **{st.session_state._diag_analysis:.1f} s**")
    _lines.append(f"- This run's Python time: **{time.perf_counter() - _T_RUN0:.2f} s**")
    _lines.append(f"- Preprocessing cache: {'hit' if st.session_state.get('_diag_pipeline_hit') else 'rebuilt'}")
    if '_diag_analysis_path' in st.session_state:
        _lines.append(f"- Last analysis path: {st.session_state._diag_analysis_path}")
    _heap = wasm_memory_mb()
    _lines.append(f"- Python (WebAssembly) memory, high-water mark: **{_heap:.0f} MB**"
                  if _heap is not None else "- Python memory: not available")
    st.markdown("\n".join(_lines))

# stlite: record default choices only after every initial widget has rendered.
remember_settings_baseline()
