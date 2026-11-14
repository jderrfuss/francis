# Francis — Advanced Data Preprocessing and Collation

**Francis** is a web app for preprocessing and collating
[PsychoPy](https://www.psychopy.org/) CSV output files into a single,
analysis-ready spreadsheet. If you have used pivot tables in Excel to aggregate
your data by condition, Francis does the same — but with a full preprocessing
pipeline built in: missing data removal, extreme value rejection, outlier
exclusion, and flexible accuracy calculations — all without writing any code.

🔗 **Live app:** https://francis.streamy.psychology.nottingham.ac.uk/

> Only need to collate data without further preprocessing?
> Try [Paco](https://paco.streamy.psychology.nottingham.ac.uk/).

## Your data stays on your computer

Francis runs entirely inside your web browser. It is a
[Streamlit](https://streamlit.io/) app that runs in the browser through
[stlite](https://github.com/whitphx/stlite) and
[Pyodide](https://pyodide.org/). The files you select are processed in the
browser tab and are never uploaded to a server.

At startup, the browser loads the app files from the Francis website (or from
your own computer, see below). It also downloads a fixed version of the Python
runtime and packages from jsDelivr and the Python Package Index. Those versions
are pinned in [`app_source/runtime-manifest.json`](app_source/runtime-manifest.json).
This version of Francis has been tested in Chrome, Brave, Safari and Firefox.
On at least some Macs connected to the University of Nottingham VPN, Firefox
cannot reach the Francis website. If this happens, use Safari or a
Chromium-based browser such as Chrome or Brave, or disconnect from the VPN
while you use Francis.

## Features

- **Collate** any number of PsychoPy CSV files from a single ZIP (or a single
  CSV) upload, including files in subfolders. A `file_name` column is added so
  rows stay traceable to their source file.
- **Global filters:** exclude files below a minimum row count (e.g. participants
  who did not finish), and keep only the rows belonging to your main
  experiment loop(s).
- **Preprocessing pipeline** (ordered, code-free blocks): derive new columns
  (arithmetic, shift, recode, concatenate, or a free-form formula), remove
  trials with missing/unwanted values, and reject extreme reaction times.
- **Reaction time analysis:** per-condition mean / median / sum / count with
  optional outlier rejection (SD, MAD, Double MAD, or percentage trimming).
- **Error rate analysis:** flexible numerator/denominator ratios for error or
  accuracy rates, with optional exclusion of no-response trials.
- **Reproducibility:** save and reload your full configuration as JSON. Each
  analysis download contains the results, a human-readable settings report and
  the exact configuration used.
- Download the results and, separately, the preprocessed trial-level data.

The [User Guide](app_source/user_guide.md) (also shown in the app) explains
every option and includes an example dataset.

## Versions

Each release of Francis is tagged with its build ID (for example
`2026-11-13.3`), which is also shown in the app's sidebar. To get the exact
version you used, open the tag on GitHub and download it as a ZIP via
**Code → Download ZIP**.

The earlier version, which ran on a server rather than in the browser, is
available under the tag
[`server-version-final`](https://github.com/jderrfuss/francis/tree/server-version-final).

## Running Francis on your own computer

You can run any version from your own computer instead of the website, for
example to keep using an exact version. You need Python 3 and a browser. An
internet connection is still needed at startup to download the runtime.

1. Download and extract this repository (or a tagged version, see above).
2. Open a terminal in the extracted folder and run:

   ```bash
   python3 -m http.server 8000 --bind 127.0.0.1 --directory app_source
   ```

   On Windows, use `py -3` in place of `python3`.

3. Open http://127.0.0.1:8000/ in your browser. Leave the terminal open while
   you use Francis, and press **Ctrl+C** to stop it.

The page must be served like this. Opening `index.html` directly does not
work, because the browser blocks it from loading the app files. Keep
`--bind 127.0.0.1`: without it, the server is reachable from other computers on
your network. If port 8000 is in use, choose another number.

## How to cite

If you use this app for your research or coursework, please cite it as follows:

> Derrfuss, J. (2026). *Francis: Advanced data preprocessing and collation*
> [Computer software]. https://francis.streamy.psychology.nottingham.ac.uk/

## Contact & feedback

Please report issues or suggestions via the
[GitHub issue tracker](https://github.com/jderrfuss/francis/issues), or contact
Jan ([jan.derrfuss@nottingham.ac.uk](mailto:jan.derrfuss@nottingham.ac.uk)).

## License

Francis is free software, licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). In short: you are
free to use, study, share, and modify it, but if you run a modified version as a
network service, you must make your modified source available to its users.
