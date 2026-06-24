This guide follows the layout of the app. The sidebar on the left handles global settings and export; the tabs on the right define how your data are preprocessed and analysed.

## Getting started

### What Francis does

Francis takes one CSV file per participant, combines them, and produces a single spreadsheet with one row per participant and one column per experimental condition. It also applies a preprocessing pipeline — removing incomplete experiments, rejecting extreme reaction times, and excluding statistical outliers — before computing the measures you ask for.

### Supported files

Francis accepts:

- **A single CSV file** — one output file for one participant. File size limit: 200 MB
- **A ZIP archive containing multiple CSV files** — the most common case. Compress all participant files into one ZIP and upload it. Files inside subfolders are included automatically; macOS metadata files (`__MACOSX`) are ignored. File size limit: 200 MB total uncompressed, 50 MB per individual uncompressed file

### Privacy notice

You must tick the privacy confirmation box before the file uploader appears. Do not upload files that contain participants' real names, email addresses, or any other unanonymised identifiers.

### How column names are handled

Francis modifies column names on load: any character that is not a letter, digit, or underscore is replaced with an underscore. In practice this means dots become underscores — so PsychoPy's `key_resp.rt` becomes `key_resp_rt` and `key_resp.corr` becomes `key_resp_corr` everywhere in the app. Keep this in mind when writing filter expressions.

---

## Example dataset

A small example dataset is available to help you explore Francis before using it on your own data.

<a href="app/static/distance_flanker_data.zip" download><strong>⬇ Download example data (.zip)</strong></a><br>
<a href="app/static/distance_flanker.json" download><strong>⬇ Download example settings (.json)</strong></a>

### The task

The example data come from an **arrow flanker task**. Participants responded to a central arrow flanked by four additional arrows. The flankers could be pointing in the same direction as the target (congruent) or the opposite direction (incongruent), and they were presented either close to the target (near) or further away from it (far). This gives a 2 × 2 design with two within-subjects factors:

- **Congruency:** congruent vs. incongruent
- **Distance:** near vs. far

Each participant completed 16 practice trials followed by 72 experimental trials. Responses and reaction times were recorded in the columns `response_corr` and `response_rt`.

### The files

The ZIP archive contains 10 PsychoPy output files — one per participant. One participant's file is incomplete (69 rows), having fewer rows than a full session (89 rows including the header). This participant is excluded automatically by the minimum row number filter configured in the example settings.

### Using the example settings

After uploading the ZIP archive, load the example `.json` settings file using **Load Configuration** in the sidebar, then click **Apply Settings**. The configuration sets up the following pipeline:

1. **Data Preprocessing** — an Extreme Value Rejection block discards trials with an RT below 0.15 s or above 2.0 s.
2. **Reaction Times** — mean correct-trial RT per condition (congruency × distance), with MAD-based outlier rejection (3 MADs), converted to milliseconds.
3. **Error Rates** — percentage accuracy per condition (congruency × distance).

Click **Run Analysis & Prepare Downloads** to produce the output CSV.

---

## Sidebar

### 1. Input Data

**Participant ID Column**
Choose the column that identifies each participant (usually `participant` in PsychoPy files). If your files do not have a participant column, choose `(None)` — rows will be labelled by the source file name.

**Group Data By**
This controls what makes up one row in the output file.

| Option | When to use |
|---|---|
| *File Name (1 file = 1 row)* | Default. Each uploaded file becomes one output row. Safest: two participants with the same ID remain separate. |
| *Participant ID (Merge files)* | Use for multi-session studies where you want a single output row per participant across sessions. All files sharing the same participant ID are merged before analysis. |

> ⚠️ If you select *Participant ID* but the Participant ID Column is set to `(None)`, Francis falls back to grouping by file name.

**Additional Columns to Keep**
Select any metadata columns you want to preserve in the output (e.g. `gender`, `age`). These appear at the left of the final spreadsheet alongside the participant ID.

---

### 2. Global Filters

Global filters remove entire rows from the dataset before any analysis runs. They are applied first — before the preprocessing blocks in the Data Preprocessing tab.

**2.1 Minimum Row Number Filter**
Enable this if a completed experiment always produces a fixed number of rows. Any file with fewer rows than the threshold is excluded entirely — a quick way to drop participants who quit early.

The count includes the header row, so it matches the last row number shown when the file is open in Excel (where row 1 is the header). The easiest way to find the right threshold is to open a complete participant file in Excel and note the row number of the last row — enter that number here.

*Example:* A complete file has a header plus 350 rows of data (instructions, practice trials, and experimental trials combined). In Excel the last row is row 351. Set the minimum to 351. Any file where the last row is earlier than 351 — such as a participant who quit during the experiment — will be excluded.

> The threshold is checked against each **file** individually — even when *Group Data By* is set to **Participant ID (Merge files)**. Rows are never summed across a participant's files before the check, so a participant whose session is split into several individually short files could be excluded. Choose a threshold that matches one complete file.

**2.2 Main Loop(s) Filter** *(PsychoPy only)*
PsychoPy writes a column called `<loopName>.thisN` for every active loop. Rows where this counter is `NaN` are not part of that loop — e.g., they might correspond to instructions, practice phases, or inter-block screens.

Select the loop(s) that correspond to your main experimental trials. Rows where none of the selected loops are active are discarded. If your data contains no `thisN` columns (e.g. non-PsychoPy files), this section will show no options and can be ignored.

*Example:* If your main trial loop is called `trials`, select `trials`. Rows from a practice loop called `practice` will be excluded.

> If you select multiple loops, a row is kept if *any* of the selected loops is active — useful for designs split across several loops that all contribute to the same analysis.

---

### 3. Reporting

**Include trial counts and outlier limits**
When checked, Francis adds diagnostic columns to the output. These are useful for checking that filtering and outlier rejection behaved as expected:

- `_trls_pre_filter` — trials matching the condition before the trial filter was applied.
- `_trls_rejected_by_filter` — trials removed by the trial filter.
- `_trls_post_filter` — trials remaining after the trial filter.
- `_trls_excluded_dv` — trials dropped because the dependent variable was missing (and, for Mean/Median with *Exclude trials where the DV is 0* enabled, zero). RT blocks only.
- `_lower` / `_upper` — the computed outlier boundaries.
- `_trls_outlier` — trials removed as outliers (RT blocks only).
- `_trls_final` — trials used to compute the final value.

For RT blocks these reconcile: `_trls_pre_filter − _trls_rejected_by_filter = _trls_post_filter`, and `_trls_post_filter − _trls_excluded_dv − _trls_outlier = _trls_final`.

Global counts are also added once per participant: `trls_total`, `trls_missing`, `trls_extreme_global`, `trls_valid_global`.

---

### 4. Export

**Save Configuration (.json)**
Downloads all current settings — filter choices, analysis blocks, grouping options — as a JSON file. Load it in a future session to restore your exact configuration without re-entering everything by hand.

**Run Analysis & Prepare Downloads**
Runs all active Reaction Time and Error Rate analysis blocks and prepares two files for download:
- A results CSV (one row per participant, one column per condition).
- A Markdown settings report documenting every analysis decision, useful for methods sections and for sharing with collaborators.

The download buttons appear below this button after the analysis completes. The files are a snapshot of the moment you clicked — the **Last generated at** caption shows when. If you change any settings (or the data) afterwards, the downloads keep serving that snapshot and a warning appears; click **Run Analysis & Prepare Downloads** again to refresh them.

> The **Preview Results** button within each analysis tab (see below) lets you inspect results for that tab only, without preparing the full export.

---

## Data Preprocessing Tab

Preprocessing blocks remove unwanted rows or create new columns. They run in the order you define them, after the Global Filters in the sidebar. Add as many blocks as needed using the **(+) Add New Block** button. Blocks can be deleted individually if more than one exists.

Six block types are available:

---

### New Column — Arithmetic

Derives a new variable from one or two existing columns using arithmetic or logarithm operations. The new column is immediately available in later blocks and in the analysis tabs.

| Operator | Inputs needed | Notes |
|---|---|---|
| **Concatenate (A_B)** | Column A, Column B | Joins the values from two columns into a single text label, separated by an underscore. |
| **Ratio (A / B)** | Column A, Column B | Divides A by B. |
| **Multiplication (A × B)** | Column A, Column B | Multiplies A by B. |
| **Sum (A + B)** | Column A, Column B | Adds A and B. |
| **Difference (A − B)** | Column A, Column B | Subtracts B from A. |
| **Log (ln A)** | Column A | Natural logarithm. `ln(0)` becomes `NaN`. |
| **Log (log10 A)** | Column A | Base-10 logarithm. `log10(0)` becomes `NaN`. |
| **Square Root (√A)** | Column A | |
| **Inverse (1/A)** | Column A | `1/0` becomes `NaN`. |
| **Absolute Value (\|A\|)** | Column A | |

**Common uses:**

- *Task-switching and N-back studies:* Use a **New Column — Shift Values** block to bring the previous trial's condition into the current row (e.g. `prev_task`), then use **Concatenate** on `task` and `prev_task` to create a combined label such as `A_A` or `A_B`. Use this new column as a grouping variable in the analysis tabs to obtain separate condition means for switch and repeat trials.
- *Log-transforming RT:* Use **Log (ln A)** on your RT column before computing means, to reduce the skew typical of RT distributions.
- *Speed (1/RT):* Use **Inverse (1/A)** to work with response speed instead of response time.

---

### New Column — Recode Values

Creates a new column by assigning values based on conditions. Each rule pairs a filter expression with an output value. Rules are evaluated top to bottom — the first matching rule wins. Rows that do not match any rule receive the default value.

**Setting up a New Column — Recode Values block:**

1. Enter a name for the new column (e.g. `switch_rep`).
2. Click **＋ Add Rule** to add a rule. Each rule has two fields:
   - **Condition** — a filter expression using the same syntax as the Trial Filter (e.g. `task == prev_task`). Multiple conditions can be combined with `and` / `or` and parentheses.
   - **Output value** — the label assigned to rows matching this condition (e.g. `repeat`).
3. Add as many rules as needed.
4. Enter a **Default value** for rows that match none of the rules (e.g. `switch`).

The resulting column is immediately available as a grouping variable in the Reaction Times and Error Rates tabs, or as an input to later preprocessing blocks.

*Example — task-switching:*

| Condition | Output value |
|---|---|
| `prev_task != prev_task` | `first_trial` |
| `task == prev_task` | `repeat` |
| *(default)* | `switch` |

Using `task` and a **Shift** block to create `prev_task` first, this produces a `switch_rep` column. The first rule catches trials with no preceding trial (the first trial in each participant's file, where `prev_task` is empty). The expression `prev_task != prev_task` is the standard way to test for an empty (missing) value: a missing value is the only thing that is not equal to itself. You can then group by `switch_rep` in the analysis tabs, and use a Trial Filter such as `switch_rep != 'first_trial'` to exclude those trials from the analysis.

*Example — response type classification:*

| Condition | Output value |
|---|---|
| `response_corr == 1` | `correct` |
| `response_corr == 0 and response_rt > 0` | `error` |
| *(default)* | `omission` |

*Example — RT binning:*

| Condition | Output value |
|---|---|
| `response_rt < 0.3` | `fast` |
| `response_rt < 0.6` | `medium` |
| *(default)* | `slow` |

Because rules are evaluated in order, a trial with RT = 0.25 matches the first rule and is labelled `fast`, never reaching the second.

> The condition syntax is identical to the Trial Filter — see the **Additional Filter — syntax reference** section for full details, including how to combine conditions with `and`, `or`, and parentheses.

---

### New Column — Shift Values

Brings the value from a neighbouring trial into the current row, creating a new column. This is useful for task-switching and N-back studies where you need the *previous* trial's condition in the current row.

| Setting | Description |
|---|---|
| **New Column Name** | Name for the derived column (e.g. `prev_task`). |
| **Column to Shift** | The column whose values are shifted. |
| **Shift Amount** | How many rows to look back or forward. **+1** = previous trial; **−1** = next trial. |

Shift is grouped by participant file, so the last trial of one participant's file never bleeds into the first trial of the next. The first trial in each file will have `NaN` in the new column (there is no preceding row to shift from).

> ⚠️ **Shift and row-removal blocks:** If a **Missing Data Removal** or **Extreme Value Rejection** block appears *before* a Shift block, the shift is computed on already-filtered data. This may cause incorrect N-1 adjacency. Francis warns you if this is the case. To avoid it, place all Shift blocks *above* any row-removal blocks.

---

### New Column — Custom Formula

Derives a new column using a free-form arithmetic expression you write yourself. Use this when the built-in Arithmetic operators are not flexible enough — for example when a formula combines more than two columns or mixes columns with fixed constants in a single expression.

**How to set it up:**

1. Enter a name for the new column (e.g. `harmonic_mean`).
2. Type an arithmetic formula using column names and numbers, e.g.:

   ```
   (2 * col_a * col_b) / (col_a + col_b)
   ```

The new column is immediately available in later preprocessing blocks and in the analysis tabs.

**Supported syntax:**

| Element | Examples |
|---|---|
| Column names | `response_rt`, `col_a` |
| Numbers | `45`, `0.5`, `72` |
| Arithmetic operators | `+`, `-`, `*`, `/`, `%` (modulo), `**` (power) |
| Parentheses | `(col_a + col_b) / 2` |
| Math functions | `sqrt(x)`, `abs(x)`, `log(x)` (natural), `log10(x)`, `exp(x)` |

> **Column names with dots:** Francis converts all dots in PsychoPy column names to underscores when loading your files. So `resp.rt` becomes `resp_rt`, and `key_resp.corr` becomes `key_resp_corr`. Use the underscore form in your formula. The block displays a reminder of this.

> **What is not supported:** String operations, conditional expressions, comparisons, and floor division (`//`). Use **New Column — Recode Values** for conditional logic.

**Example:**

- *Harmonic mean of two RT columns:* `(2 * col_a * col_b) / (col_a + col_b)`

---

### Missing Data Removal

Removes entire trials (rows) where a selected column contains an unwanted value. You choose which columns to check and which values count as "missing":

- **NaN (missing):** Removes rows where the column has no value. In PsychoPy, trials where the participant gave no response within the time limit leave `NaN` in the RT column.
- **Zero (0):** Removes rows where the column value is exactly 0. Useful for RT columns — an RT of 0 is physically impossible and usually indicates a recording error.
- **Other value(s):** Enter any additional values to treat as missing, separated by commas (e.g. `999, -1`). Numbers are matched numerically; text is matched exactly.

A trial is removed if *any* of the selected conditions is met in *any* of the selected columns.

> **Tip — accuracy analyses:** Trials removed here are absent from *all* subsequent analyses, including error rate denominators. If you want no-response trials to remain in error rate calculations (so they can be counted as errors or excluded selectively), use the **Exclude trials with no response** option in the Error Rates tab instead of removing them here.

---

### Extreme Value Rejection

Removes trials where a numeric column (typically RT) falls outside a defined range.

| Setting | Meaning |
|---|---|
| RT Column | The column to apply the bounds to. |
| Min RT | Trials *faster* than this value are discarded. |
| Max RT | Trials *slower* than this value are discarded. |

Rows where the selected column is `NaN` — or holds a non-numeric value, which is treated as `NaN` — are **not** removed by this block. Only numeric values that fall outside the range are discarded; missing values are handled by a Missing Data Removal block, not here.

*Example:* Set Min RT to `0.15` and Max RT to `4.0` to discard implausibly fast responses and trials where the participant was very slow (assuming your data are in seconds).

> **Units:** Francis does not convert units during preprocessing. If your data are in seconds, set Min and Max in seconds. If they are in milliseconds, set them in milliseconds.

> **Scope:** Extreme value rejection is a global preprocessing step — it removes rows from the dataset for *all* subsequent analyses, both RT and Error Rate. This means those trials are also absent from error rate denominators. For example, a very slow correct response that exceeds Max RT will not be counted in the error rate denominator. If this is undesirable, omit this block and apply the bounds only within specific RT analysis blocks using the **Trial Filter** field (e.g. `key_resp_rt >= 0.15 and key_resp_rt <= 4.0`).

**Block ordering matters:** Place Extreme Value Rejection blocks after any New Column — Arithmetic blocks whose output you still want to be available (columns created before a row-removal block survive into subsequent blocks). Place them before the analysis tabs run.

---

### Downloading the Preprocessed Dataset

At the bottom of the tab, **Prepare Preprocessed Data (.csv)** builds a CSV of the full trial-level dataset after all global filters and preprocessing blocks have been applied — useful for checking what your pipeline actually did, or for continuing in another tool. Click it, and a download button appears in its place, with a caption showing when the file was prepared. If you change the data or any preprocessing setting afterwards, the prepared file is discarded and the button reverts to **Prepare** — so the file you download always matches the current pipeline.

---

## Reaction Times Tab

Each **Analysis Block** computes a summary measure (mean, median, sum, or count) for each combination of condition values. Add additional blocks using **(+) Add New Analysis Block** if you need to analyse the same or different variables with different settings.

Each block has five sections:

---

### Define Conditions

**Select columns to split the summary measure into separate conditions**
Choose one or more columns whose unique values define your experimental conditions. Francis produces one output column per unique value (or combination of values).

*Example:* Select `congruency` with values `congruent` and `incongruent`. Francis produces two output columns — one mean RT per condition.

Select `(None)` to compute a single summary across all trials, without splitting by condition.

**Include column names in output**
When condition values are numbers, they can be hard to identify in the output. Ticking this adds the column name as a prefix.

*Example:* Assume your goal is to get per-block mean RTs. The relevant column is called `block`, the values are 1 to N. Without this option, the first block produces a column called `1_key_resp_rt_mean` — not very useful. With it, the column is called `block1_key_resp_rt_mean`.

---

### Select Dependent Variable

Choose the column to summarise (typically an RT column). The **Show only RT columns** toggle shortens the list to columns ending in `_rt` or `.rt`. Uncheck it to see all columns.

---

### Exclude Trials from Analysis

**Trial Filter**
A pandas query expression applied within each condition, after all global and preprocessing filters. Use this to restrict the analysis to specific trials.

*Examples:*
- `key_resp_corr == 1` — include only correct trials (useful for computing correct-trial RT).
- `trials_thisN >= 4` — skip the first four trials of the loop (remember that the first trial in the block will be number 0).

See the **Additional Filter — syntax reference** section below for full details.

**Outlier Rejection**
Enable with the **Enable outlier rejection** checkbox. Two settings appear:

*Scope:*
- **Per Condition** — outlier bounds are calculated from the data within each condition separately.
- **Global (Participant Level)** — bounds are calculated from all of a participant's valid trials for the dependent variable (or from trials matching the Trial Filter, if one is set). The same bounds are then applied to every condition. Use this when you want a single, stable reference per participant regardless of condition.

*Method:*
| Method | Threshold parameter | Notes |
|---|---|---|
| Standard Deviation (SD) | Number of SDs from the mean |  |
| Median Absolute Deviation (MAD) | Number of MADs from the median | More robust to extreme values in the distribution itself. |
| Double MAD | Number of MADs (asymmetric) | Separate upper and lower bounds. Well-suited to skewed RT distributions. |
| Percentage Trimming | % trimmed from each end | Removes the fastest X% and slowest X% of trials symmetrically. |

> Outlier rejection is skipped for any condition with fewer than 4 valid trials. These are listed in a warning after the analysis runs.

**Condition Name Suffix**
An optional short label appended to every output column name for this block. Useful to distinguish two blocks that analyse the same conditions differently.

*Example:* If you have one block for all trials and one for correct trials only, add `_correct` as the suffix for the second block, giving output columns like `congruent_key_resp_rt_mean_correct`.

---

### Summary Measure

**Measures:**

| Measure | Description |
|---|---|
| Mean | Arithmetic mean. Trials with `NaN` are excluded; trials with a value of 0 are excluded by default (see below). |
| Median | Median. Trials with `NaN` are excluded; trials with a value of 0 are excluded by default (see below). |
| Sum | Sum of all values. **Includes zeros** — valid for cumulative scores or questionnaire totals where 0 is a meaningful response. |
| Count | Number of valid trials remaining after all filtering. Trials with `NaN` in the DV do not count. |

> Negative values are always included, for all measures. Log-transformed RTs and difference scores are legitimately negative, so they enter the summary like any other value.

> If there is only a single observation per condition, Mean, Median, and Sum all return that value directly.

**Exclude trials where the DV is 0** *(Mean and Median only)*
Ticked by default. For raw RT columns, an RT of 0 is physically impossible and usually indicates a recording error, so such trials are excluded before the summary measure and the outlier bounds are computed. Untick this if 0 is a meaningful value for your dependent variable — for example difference scores, or log-transformed values (ln of an RT of exactly 1 s is 0). Zeros are always included for Sum and Count, where 0 is often a valid data point (e.g. a Likert scale item scored 0).

**Convert s to ms**
If your RT column is in seconds (PsychoPy's default), tick this to multiply the output values by 1,000. Applies to Mean and Median only; outlier bounds are also converted.

**Minimum trials per condition**
If the number of valid trials in a condition — after all filtering and outlier rejection — falls below this value, the result is set to `NaN` rather than computing a potentially unreliable summary. Default is 1 (no minimum enforced).

---

### Conditions Preview

Once you have selected at least one grouping column and a dependent variable, a preview table appears showing the output column names Francis will produce and the filter logic used for each condition. Use this to verify that conditions are defined as intended before running the analysis.

**▶ Preview Results** — runs the RT analysis and displays the results in a table below the blocks. Use this to check your analysis interactively. The full export (combined with Error Rates) is prepared by clicking on the **Run Analysis & Prepare Downloads** button in the sidebar.

---

## Error Rates Tab

Each **Analysis Block** computes a ratio: the number of trials matching a numerator criterion divided by the number of trials matching a denominator criterion. The result can represent error rates, accuracy rates, or any other proportion.

Each block has five sections:

---

### Define Conditions

Works identically to the RT tab. Select the column(s) whose values define your conditions, or select `(None)` for an overall rate across all trials.

**Include column names in output** also works the same way as in the RT tab.

---

### Select Target Column

Choose the column whose values classify each trial. In PsychoPy this is typically the `.corr` column (e.g. `key_resp_corr`), which records `1` for a correct response and `0` for an incorrect one. The **Show only accuracy columns** toggle shortens the list to columns ending in `_corr` or `.corr`.

You can use any column here — Francis is not limited to PsychoPy accuracy columns. For example, you could use a condition column to compute what proportion of trials were in a given condition.

---

### Define Accuracy

Specify which values in the target column count as the event you are measuring (numerator) and which values define the pool of relevant trials (denominator). Separate multiple values with commas.

**Numerator values** — the value(s) that represent the event of interest.
**Denominator values** — the value(s) that define the total pool.

*Common PsychoPy examples:*

| Goal | Numerator | Denominator |
|---|---|---|
| Error rate | `0` | `0, 1` |
| Accuracy rate | `1` | `0, 1` |

> String values do not need quotes — Francis handles quoting automatically. So to match a column containing the text `error`, simply enter `error`.

**Exclude trials with no response** *(PsychoPy only)*
In PsychoPy, when a participant does not respond within the time limit, the accuracy column records `0` — identical to an incorrect response. To distinguish non-responses from errors, Francis looks at the paired RT column (e.g. `key_resp_rt` for `key_resp_corr`): a trial with `NaN` in the RT column but `0` in the accuracy column is a non-response.

When this option is ticked, those non-response trials are excluded from both the numerator and the denominator. This is appropriate when you want to measure *error rate on trials where a response was given*, not counting failures to respond as errors.

When unticked (default), all trials with the target column values you specified are included, regardless of whether a response was recorded.

> This option relies on PsychoPy's naming convention (`_corr` and `_rt` suffixes on the same response component). If the paired RT column cannot be found, a warning appears and the option has no effect.

---

### Exclude Trials from Analysis

An optional pandas query expression applied before counting, equivalent to the Trial Filter in the RT tab.

*Example:* `block == 2` — only count trials from block 2

See the **Additional Filter — syntax reference** section below.

---

### Output

**Condition Name Suffix** — an optional label appended to all output column names for this block (e.g. `_err` or `_acc`).

**Convert to %** — when checked, the ratio is multiplied by 100. So 0.125 becomes 12.5.

**Minimum trials per condition** — if the number of trials in the denominator for a condition falls below this value, the result is set to `NaN`. This is the denominator count (all trials with values matching your denominator specification), not the numerator count.

---

### Conditions Preview

Appears once you have selected at least one grouping column and a target column. Shows the output column names and the filter logic for each condition.

**▶ Preview Results** — runs the Error Rate analysis and displays results below the blocks.

---

## Additional Filter — syntax reference

The Trial Filter and Exclude Trials fields accept a **pandas query expression** — a concise way to select rows using column names and comparison operators. Here are the most useful patterns:

> For safety, these fields accept only column names, numbers, text values, comparisons (`==`, `!=`, `<`, `>`, `<=`, `>=`), membership tests (`in [...]`), arithmetic, and the connectors `and` / `or` / `not` with parentheses. Method calls (e.g. `.str.contains(...)`) and attribute access are not permitted.

### Basic examples

| Expression | Effect |
|---|---|
| `key_resp_corr == 1` | Keep only correct trials |
| `key_resp_corr == 0` | Keep only error trials |
| `block == 2` | Keep only rows where `block` equals 2 |
| `block in [1, 2, 3]` | Keep rows where `block` is 1, 2, or 3 |
| `condition == 'incongruent'` | Keep rows where `condition` is the string `incongruent` |
| `key_resp_rt >= 0.2` | Keep rows with RT ≥ 0.2 s |
| `trials_thisN >= 4` | Skip the first four trials in the loop (PsychoPy; 0-indexed) |

### Combining conditions

Use `and` and `or` with parentheses:

```
(block == 2) and (key_resp_corr == 1)
```

```
(condition == 'switch') or (condition == 'repeat')
```

### Column name rules

- Dots in original column names are replaced with underscores on load — use `key_resp_rt`, not `key_resp.rt`.
- Column names with spaces are also converted to underscores.

### Common mistakes

| Mistake | Correct form |
|---|---|
| `condition = 'switch'` (one `=`) | `condition == 'switch'` (two `==`) |
| `block = 1 or 2` | `block == 1 or block == 2` or `block in [1, 2]` |

---

## Saving and reusing settings

After configuring your analysis, click **Save Configuration** in the sidebar to download a `.json` file. This stores all widget states — analysis blocks, filter choices, column selections, and outlier settings — so you can reproduce the exact same analysis later or share it with a collaborator.

To reload a configuration in a future session:

1. Upload your data as usual.
2. Expand **Load Configuration** in the sidebar (visible after data is uploaded).
3. Upload the `.json` file.
4. Click **Apply Settings**.

> If columns referenced in the configuration are missing from the current dataset (e.g. a different experiment version), Francis warns you and skips those settings rather than crashing.

---

## Output file structure

The downloaded CSV has this column order:

1. **Participant/metadata** — participant ID, any Additional Columns to Keep, source file name.
2. **Global trial counts** — `trls_total`, `trls_missing`, `trls_extreme_global`, `trls_valid_global` (only if *Include trial counts* is checked).
3. **Analysis results** — one column per condition per analysis block, from RT blocks first then Error Rate blocks.
4. **Diagnostic counts** — per-condition counts and outlier bounds (only if *Include trial counts* is checked).

---

## Frequently asked questions

**My filter expression doesn't work — the column name looks right.**

Check for dots. Francis converts dots to underscores on load, so `key_resp.rt` in your original file becomes `key_resp_rt` in the app. Use underscores in all filter expressions.

**The output has `NaN` for some participants in a condition.**

Either (a) the participant had no trials matching that condition after filtering, or (b) the number of valid trials fell below the **Minimum trials per condition** threshold. Enable *Include trial counts* in the sidebar and re-run to see the `_trls_final` column, which shows exactly how many trials were used.

**My error rate denominator is lower than expected.**

Check whether a **Missing Data Removal** block is removing no-response trials. If non-responses should enter the denominator (so they can be counted as errors), remove the RT column from the Missing Data Removal block. Then use the **Exclude trials with no response** option in the Error Rates tab to handle non-responses selectively: untick it to count them as errors, or tick it to exclude them from both numerator and denominator.

**Outlier rejection was skipped for some conditions.**

Francis requires at least 4 valid trials to compute reliable outlier bounds. Conditions below this threshold are listed in a warning after the analysis runs.

**I want to apply extreme-value bounds to RT analyses only, not to error rate denominators.**

Omit the Extreme Value Rejection block from the Data Preprocessing tab. Instead, add a Trial Filter expression inside each RT block (e.g. `key_resp_rt >= 0.15 and key_resp_rt <= 4.0`). The bounds will apply only to those RT analyses and will not affect error rate counts.

**Can I compute something other than error rates in the Error Rates tab?**

Yes. The tab computes any ratio of the form *numerator trials ÷ denominator trials*. For example, to compute the proportion of trials that were incongruent, set the target column to `congruency`, numerator to `incongruent`, and denominator to `congruent, incongruent`.
