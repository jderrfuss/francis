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
- **Reproducibility:** save and reload your full configuration as JSON, and
  export a human-readable settings report alongside the results.
- Download the collated results, the preprocessed data, and the settings report.

## Running locally

Francis is a single-file [Streamlit](https://streamlit.io/) app.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (typically http://localhost:8501).

The maximum upload size is configured in `.streamlit/config.toml`
(`maxUploadSize`, in MB). The uncompressed safety caps (zip-bomb / OOM
protection) live in `load_data()` in `app.py`.

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
