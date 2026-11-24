"""Francis browser analysis: whole-frame filters, original-order reductions.

Derived from stress_test/groupby_poc.py. Yielded checkpoints let stlite service
progress and Cancel. Errors propagate to app.py's untouched calculation rules;
this module never coerces or discards incompatible filter operands.
Copyright (C) 2026 Jan Derrfuss. SPDX-License-Identifier: AGPL-3.0-or-later.
"""
import numpy as np
import pandas as pd

QUIET_ERRORS = (pd.errors.UndefinedVariableError, ValueError, SyntaxError)
# Dependent variable "(None)": Count the trials in each condition, whatever
# their values. Only Count is offered for it.
NO_DV = "(None)"

def _bounds(v, codes, method, thresh):
    """Lower/upper bounds per group, mirroring app.py's calculate_*_bounds."""
    g = v.groupby(codes)
    if method == "Standard Deviation (SD)":
        mean, sd = g.mean(), g.std()
        lower, upper = mean - thresh * sd, mean + thresh * sd
        zero = sd == 0
        lower[zero], upper[zero] = -np.inf, np.inf
    elif method == "Median Absolute Deviation (MAD)":
        med = g.median()
        mad = 1.4826 * (v - med.reindex(codes).to_numpy()).abs().groupby(codes).median()
        lower, upper = med - thresh * mad, med + thresh * mad
        zero = mad == 0
        lower[zero], upper[zero] = -np.inf, np.inf
    elif method == "Double MAD":
        med = g.median()
        med_row = med.reindex(codes).to_numpy()
        left = v <= med_row
        right = v >= med_row
        mad_l = 1.4826 * (v[left] - med_row[left.to_numpy()]).abs().groupby(codes[left.to_numpy()]).median()
        mad_r = 1.4826 * (v[right] - med_row[right.to_numpy()]).abs().groupby(codes[right.to_numpy()]).median()
        mad_l, mad_r = mad_l.reindex(med.index), mad_r.reindex(med.index)
        lower = (med - thresh * mad_l).where(mad_l != 0, -np.inf)
        upper = (med + thresh * mad_r).where(mad_r != 0, np.inf)
    else:  # Percentage Trimming
        lower, upper = g.quantile(thresh / 100.0), g.quantile(1 - thresh / 100.0)
    return lower, upper

def _bounds_exact(ns, v, codes, method, thresh):
    """Same as _bounds, but by calling app.py's own calculate_*_bounds on each
    participant's values (in the original row order), so every float is
    bit-identical to the original. Still one cheap call per participant."""
    f = {"Standard Deviation (SD)": ns["calculate_sd_bounds"],
         "Median Absolute Deviation (MAD)": ns["calculate_mad_bounds"],
         "Double MAD": ns["calculate_double_mad_bounds"]}.get(method, ns["calculate_trimmed_bounds"])
    lo, up = {}, {}
    grouped = v.groupby(codes, sort=False)
    for i, (code, s) in enumerate(grouped):
        if i % 32 == 0:
            yield i, grouped.ngroups, "Calculating participant statistics"
        lo[code], up[code] = f(s, thresh)
    return pd.Series(lo, dtype=float), pd.Series(up, dtype=float)

def _measure_exact(v, codes, measure, n_groups):
    # Preserve scalar types too: float arrays would turn integer sums into
    # decimal CSV values (and lose precision for sufficiently large integers).
    empty_value = v.iloc[:0].sum() if measure == "Sum" else np.nan
    out = [empty_value for _ in range(n_groups)]
    grouped = v.groupby(codes, sort=False)
    for i, (code, s) in enumerate(grouped):
        if i % 32 == 0:
            yield i, grouped.ngroups, "Calculating participant statistics"
        out[code] = s.mean() if measure == "Mean" else s.median() if measure == "Median" else s.sum()
    return out

def _count(codes, n_groups):
    return np.bincount(codes, minlength=n_groups) if len(codes) else np.zeros(n_groups, dtype=np.int64)

def analyze_vectorised_steps(ns, df, blocks, group_col, keep_cols, settings, mode="RT", name_col='file_name',
                       exact=True):
    st = ns["st"]
    # No speculative warnings: the original path reports them if this fails.
    def safe_query(frame, expression):
        if not ns["_check_expr_safety"](expression):
            raise ValueError("Filter requires legacy error reporting")
        return frame.query(expression, local_dict={}, global_dict={})
    if not blocks:
        return pd.DataFrame()

    # Condition names: identical to the original.
    final_configs, tracker = [], {}
    for row in blocks:
        row = dict(row)
        base = row['Condition_Name']
        if base in tracker:
            tracker[base] += 1
            row['Condition_Name'] = f"{base}_{tracker[base]}"
        else:
            tracker[base] = 1
        final_configs.append(row)
    # Read each condition's settings exactly as the original does: as rows of
    # one DataFrame, where a setting missing from some blocks becomes NaN
    # (which .get() returns, and which counts as true).
    final_configs = [r for _, r in pd.DataFrame(final_configs).iterrows()]

    if not df.index.is_unique:
        df = df.reset_index(drop=True)
    grouped = df.groupby(group_col)
    sizes = grouped.size()                       # same order as iterating the groupby
    pids = list(sizes.index)
    G = len(pids)
    # Group number per row; rows with no group key (NaN) get -1. (pandas 2.3
    # returns NaN for them, which would make the codes float.)
    code_of = pd.Series(grouped.ngroup().fillna(-1).astype(np.int64).to_numpy(), index=df.index)

    def codes_for(frame):
        c = code_of.loc[frame.index].to_numpy()
        return c[c >= 0]

    # Trial funnel: per-block, per-unit counts for the summary shown after a
    # preview. Purely informational; any failure here only drops the summary.
    unit_codes = code_of.to_numpy()
    funnel, funnel_ok = {}, True

    def funnel_block(row):
        key = int(row['block_num'])
        if key in funnel:
            return funnel[key]
        # Trials a block can use: a unit (participant/file) and, when grouped,
        # values in every grouping column (conditions skip missing values).
        gcols = row.get('group_cols')
        mask = unit_codes >= 0
        if isinstance(gcols, list) and gcols:
            mask = mask & df[gcols].notna().all(axis=1).to_numpy()
        extra = row.get('Extra_Logic')
        extra = extra.strip() if isinstance(extra, str) else ''
        # RT blocks record the user's filter and the error-trial exclusion
        # separately; Extra_Logic is their combination.
        text = row.get('Text_Filter')
        text = text.strip() if isinstance(text, str) else extra
        errors = row.get('Error_Filter')
        errors = errors.strip() if isinstance(errors, str) else ''
        zeros = np.zeros(G, dtype=np.int64)
        base = _count(unit_codes[mask], G)
        entry = {"base": base, "has_filter": bool(text)}
        if mode == "RT":
            entry.update(post=zeros, kept_dv=zeros, final=zeros, outliers=False, exclude_zero=False,
                         error_step=bool(errors),
                         after_errors=_count(codes_for(safe_query(df[mask], errors)), G) if errors else base)
        else:
            # Same order as the calculation: no-response trials, then the filter.
            base_df = df[mask]
            entry.update(no_response=False, no_resp=zeros, den=zeros)
            rt_col = row.get('rt_col_for_timeouts')
            if isinstance(rt_col, str) and rt_col and rt_col in df.columns:
                missing = pd.to_numeric(base_df[rt_col], errors='coerce').isna()
                entry.update(no_response=True, no_resp=_count(codes_for(base_df[missing]), G))
                base_df = base_df[~missing]
            entry["post"] = _count(codes_for(safe_query(base_df, extra) if extra else base_df), G)
        funnel[key] = entry
        return entry

    # Per-participant keep-column values.
    keep_vals = {}
    for k in keep_cols:
        if k not in df.columns:
            continue
        if k == name_col and k == group_col:
            keep_vals[k] = [str(p) for p in pids]
        elif k == name_col:
            sub = df[[group_col, k]].dropna(subset=[k]).drop_duplicates()
            joined = sub.groupby(group_col)[k].agg(lambda s: ' | '.join(str(f) for f in s))
            keep_vals[k] = [joined.get(p, '') for p in pids]
        else:
            first = df.loc[code_of >= 0].drop_duplicates(subset=[group_col])   # first row of each group
            fmap = dict(zip(first[group_col], first[k]))
            keep_vals[k] = [fmap[p] for p in pids]

    # Per-condition results, as plain per-participant lists.
    per_cond = []
    skipped_items_by_pid = [[] for _ in range(G)]
    for condition_i, row in enumerate(final_configs):
        yield condition_i, len(final_configs), "Analysing conditions"
        c_name = row['Condition_Name']
        # stage: how far the computation got before an error, so that the
        # output keeps exactly the count columns the original had already
        # written when it failed (1 = filter counts, 2 = + excluded_dv).
        r = {"row": row, "c_name": c_name, "failed": False, "stage": 0}
        per_cond.append(r)
        try:
            if mode == "RT":
                c_dv, c_meas = row['Dependent_Var'], row['Measure']
                no_dv = c_dv == NO_DV
                if no_dv:
                    c_meas = "Count"
                # Per-group to_numeric can infer different types for text DVs.
                # The original path preserves those coercion/precision rules.
                elif not pd.api.types.is_numeric_dtype(df[c_dv].dtype):
                    raise ValueError("Dependent variable requires per-participant coercion")
                do_outlier = bool(row.get('enable_outliers', False)) and not no_dv
                o_scope = row.get('outlier_scope', 'Per Condition')
                o_method = row.get('outlier_method', 'Standard Deviation (SD)')
                o_thresh = row.get('outlier_thresh', 2.5)
                o_base_logic = row.get('Extra_Logic', '')
                excl_zero = c_meas in ["Mean", "Median"] and row.get('exclude_zero_dv', True)

                group_cols = row.get('group_cols')
                ungrouped = isinstance(group_cols, list) and not group_cols
                trial_filter = row['Filter_Logic']
                if ungrouped and not trial_filter:
                    cond = df
                    r["post"] = sizes.to_numpy()
                else:
                    cond = safe_query(df, trial_filter)
                    r["post"] = _count(codes_for(cond), G)
                r["pre"] = None
                if settings['show_counts']:
                    if ungrouped:
                        r["pre"] = sizes.to_numpy()
                    elif row.get('Factor_Logic'):
                        r["pre"] = _count(codes_for(safe_query(df, row['Factor_Logic'])), G)
                r["stage"] = 1
                # No DV: every trial in the condition counts.
                v = (pd.Series(1.0, index=cond.index) if no_dv
                     else pd.to_numeric(cond[c_dv], errors='coerce'))
                keep = v.notna()
                if excl_zero:
                    keep &= v != 0
                v = v[keep]
                vc = codes_for(v)
                v = v[code_of.loc[v.index].to_numpy() >= 0]
                n_before = _count(vc, G)
                r["n_before"] = n_before
                r["stage"] = 2

                has_bounds = np.zeros(G, dtype=bool)
                lower = np.full(G, np.nan)
                upper = np.full(G, np.nan)
                if do_outlier:
                    if o_scope.startswith("Global"):
                        base_df = df
                        if o_base_logic.strip():
                            try:
                                base_df = safe_query(df, o_base_logic)
                            except Exception:
                                raise
                        vb = pd.to_numeric(base_df[c_dv], errors='coerce').dropna()
                        if excl_zero:
                            vb = vb[vb != 0]
                        vb = vb[code_of.loc[vb.index].to_numpy() >= 0]
                        bc = codes_for(vb)
                        eligible = _count(bc, G) >= 4
                        if eligible.any():
                            sel = eligible[bc]
                            lo, up = ((yield from _bounds_exact(ns, vb[sel], bc[sel], o_method, o_thresh)) if exact
                                      else _bounds(vb[sel], bc[sel], o_method, o_thresh))
                            lower[lo.index], upper[up.index] = lo.to_numpy(), up.to_numpy()
                        tag = " (Global)"
                    else:
                        eligible = n_before >= 4
                        if eligible.any():
                            sel = eligible[vc]
                            lo, up = ((yield from _bounds_exact(ns, v[sel], vc[sel], o_method, o_thresh)) if exact
                                      else _bounds(v[sel], vc[sel], o_method, o_thresh))
                            lower[lo.index], upper[up.index] = lo.to_numpy(), up.to_numpy()
                        tag = ""
                    has_bounds = eligible
                    for i in np.flatnonzero(~eligible):
                        skipped_items_by_pid[i].append(f"{pids[i]}: {c_name}{tag}")
                    # Apply bounds only where a participant has them.
                    row_lo, row_up = lower[vc], upper[vc]
                    inside = (v.to_numpy() >= row_lo) & (v.to_numpy() <= row_up)
                    kept = np.where(has_bounds[vc], inside, True)
                    v, vc = v[kept], vc[kept]
                r.update(has_bounds=has_bounds, lower=lower, upper=upper, do_outlier=do_outlier)

                n_final = _count(vc, G)
                r["n_final"] = n_final
                g = v.groupby(vc)
                if exact and c_meas in ("Mean", "Median", "Sum"):
                    agg = pd.Series((yield from _measure_exact(v, vc, c_meas, G)))
                elif c_meas == "Mean":
                    agg = g.mean().reindex(range(G))
                elif c_meas == "Median":
                    agg = g.median().reindex(range(G))
                elif c_meas == "Sum":
                    agg = g.sum().reindex(range(G), fill_value=0)
                else:
                    agg = pd.Series(n_final, index=range(G))
                r["agg"] = agg.to_numpy()
                try:
                    fb = funnel_block(row)
                    fb["post"] = fb["post"] + r["post"]
                    fb["kept_dv"] = fb["kept_dv"] + n_before
                    fb["final"] = fb["final"] + n_final
                    fb["outliers"] = fb["outliers"] or bool(do_outlier)
                    fb["exclude_zero"] = fb["exclude_zero"] or bool(excl_zero)
                except Exception:
                    funnel_ok = False
            else:  # Error
                err_base = df
                rt_col = row.get('rt_col_for_timeouts', '')
                if rt_col and rt_col in err_base.columns:
                    err_base = err_base[pd.to_numeric(err_base[rt_col], errors='coerce').notna()]
                denom = safe_query(err_base, row['Filter_Logic_Denominator'])
                num = safe_query(denom, row['Numerator_Logic'])
                r["den"] = _count(codes_for(denom), G)
                r["num"] = _count(codes_for(num), G)
                try:
                    fb = funnel_block(row)
                    fb["den"] = fb["den"] + r["den"]
                except Exception:
                    funnel_ok = False
        except Exception:
            raise  # wrapper restarts the original calculation, preserving errors

    # Assemble rows with the original's exact keys, order, rounding and types.
    results, skipped_items = [], []
    pp_missing = st.session_state.get('_pp_missing_removed', {})
    pp_extreme = st.session_state.get('_pp_extreme_removed', {})
    pp_total = st.session_state.get('_pp_total_counts', {})
    for i, sub_id in enumerate(pids):
        if i % 32 == 0:
            yield i, G, "Assembling participant results"
        row_res = {group_col: sub_id}
        for k, vals in keep_vals.items():
            row_res[k] = vals[i]
        if settings['show_counts']:
            row_res["trls_missing"] = pp_missing.get(sub_id, 0)
            row_res["trls_extreme_global"] = pp_extreme.get(sub_id, 0)
        if mode == "RT" and settings['show_counts']:
            row_res["trls_total"] = pp_total.get(sub_id, int(sizes.iloc[i]))
            row_res["trls_valid_global"] = int(sizes.iloc[i])
        for r in per_cond:
            row, c_name = r["row"], r["c_name"]
            if r["failed"]:
                if mode == "RT" and settings['show_counts'] and r["stage"] >= 1:
                    n_post = int(r["post"][i])
                    if r["pre"] is not None:
                        row_res[f"{c_name}_trls_pre_filter"] = int(r["pre"][i])
                        row_res[f"{c_name}_trls_rejected_by_filter"] = int(r["pre"][i]) - n_post
                    row_res[f"{c_name}_trls_post_filter"] = n_post
                    if r["stage"] >= 2:
                        row_res[f"{c_name}_trls_excluded_dv"] = n_post - int(r["n_before"][i])
                row_res[c_name] = np.nan
                continue
            if mode == "RT":
                n_post = int(r["post"][i])
                if settings['show_counts']:
                    if r["pre"] is not None:
                        n_pre = int(r["pre"][i])
                        row_res[f"{c_name}_trls_pre_filter"] = n_pre
                        row_res[f"{c_name}_trls_rejected_by_filter"] = n_pre - n_post
                    row_res[f"{c_name}_trls_post_filter"] = n_post
                n_before = int(r["n_before"][i])
                if settings['show_counts']:
                    row_res[f"{c_name}_trls_excluded_dv"] = n_post - n_before
                if r["do_outlier"]:
                    L_disp, U_disp = np.nan, np.nan
                    if r["has_bounds"][i]:
                        L_disp, U_disp = r["lower"][i], r["upper"][i]
                        if settings['show_counts']:
                            row_res[f"{c_name}_trls_outlier"] = n_before - int(r["n_final"][i])
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
                n_final = int(r["n_final"][i])
                if settings['show_counts']:
                    row_res[f"{c_name}_trls_final"] = n_final
                try:
                    c_meas = "Count" if row['Dependent_Var'] == NO_DV else row['Measure']
                    # A count of 0 is a result, not too little data: the
                    # minimum applies to Mean, Median and Sum only.
                    if c_meas != "Count" and n_final < row.get('min_trials', 1):
                        val = np.nan
                    else:
                        val = n_final if c_meas not in ("Mean", "Median", "Sum") else r["agg"][i]
                        if row.get('convert_to_ms', False) and c_meas in ["Mean", "Median"]:
                            val *= 1000
                            val = round(val)   # ValueError for NaN (no trials, minimum 0)
                except QUIET_ERRORS:
                    val = np.nan               # as the original's except clause
                row_res[c_name] = val
            else:
                den, num = int(r["den"][i]), int(r["num"][i])
                if den < row.get('min_trials', 1):
                    row_res[c_name] = np.nan
                    if settings['show_counts']:
                        row_res[f"{c_name}_denom"] = den
                        row_res[f"{c_name}_num"] = np.nan
                else:
                    ratio = num / den
                    row_res[c_name] = round(ratio * 100, 2) if row.get('Scale_To_Pct', True) else round(ratio, 5)
                    if settings['show_counts']:
                        row_res[f"{c_name}_denom"] = den
                        row_res[f"{c_name}_num"] = num
        results.append(row_res)
        skipped_items.extend(skipped_items_by_pid[i])

    if skipped_items:
        st.warning(f"⚠️ Outlier rejection skipped for {len(skipped_items)} condition(s) due to insufficient data (N < 4).")
        with st.expander("See affected conditions (Participant : Condition)"):
            st.dataframe(pd.DataFrame(skipped_items, columns=["Skipped Conditions"]), hide_index=True)
    if funnel_ok and funnel:
        st.session_state[f"_trial_funnel_{mode}"] = {"units": list(pids), "blocks": funnel}
    return pd.DataFrame(results)
