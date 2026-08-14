# influence-network
Capstone project investigating the role of the dark money network and its affect on US national policy.

We're pulling together IRS Form 990 filings, FEC campaign finance data, Senate
lobbying disclosures, and Congress.gov bill data to see if we can trace how
money and lobbying actually connect to policy outcomes. See
`influence_network.md` for the full project writeup (motivation, methodology,
data sources, prior work).

## Architecture

High level: pull four public data sources, parse and link them, store everything
in one SQLite database, and explore it through a Streamlit dashboard.

```mermaid
flowchart TB
    subgraph Sources["Public Data Sources"]
        IRS["IRS 990<br/>Nonprofit filings"]
        FEC["FEC<br/>Campaign finance"]
        LDA["LDA<br/>Lobbying disclosures"]
        CONG["Congress<br/>Bills"]
    end

    subgraph Pipeline["Extraction &amp; Processing"]
        EXTRACT["Fetch &amp; parse<br/>(extract/ pipeline)"]
        MATCH["Entity matching<br/>(link orgs across sources)"]
    end

    DB[("SQLite Database<br/>irs990_full.db")]

    subgraph App["Streamlit Dashboard"]
        OV["Overview &amp; orgs"]
        NET["Grant / personnel networks"]
        POL["Political spending &amp; lobbying"]
        LINK["Org &rarr; Bill policy links"]
    end

    IRS --> EXTRACT
    FEC --> EXTRACT
    LDA --> EXTRACT
    CONG --> EXTRACT

    EXTRACT --> DB
    DB --> MATCH --> DB
    DB --> App
```

Run the dashboard with `streamlit run app/streamlit_app.py`.
## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your API keys
```

Congress.gov and Senate LDA both work fine without a key at low volume; FEC
falls back to `DEMO_KEY` if you don't set one. See `.env.example` for details.

## Repo layout

- `extract/` - the actual ETL pipeline (collectors for Congress, FEC, LDA,
  and IRS 990 parsing), plus a SQLite schema in `db.py`. Runnable via
  `python -m extract.run <command>` - see the docstring at the top of
  `extract/run.py` for examples.
- `analysis/` - bill-text/lobbying-text alignment (TF-IDF or sentence
  embeddings) and other analysis helpers.
- `notebooks/` - exploratory and demo notebooks (quickstart, extraction
  smoke test, alignment demo, PDF similarity, etc.).
- `test_data_source_connections/` - early one-off notebooks used to poke at
  each API and see what the raw data actually looks like before we built the
  real extractors. See the README in there for more.
- `tests/fixtures/` - small hand-made sample files (e.g. a fake 990 XML) used
  to sanity-check the parsers without needing real bulk data.
- `data/` - gitignored; this is where extracted/downloaded data lands
  locally.


## IRS 990 storage and joins

The canonical IRS import tables are filing-scoped: `irs990_filings` and its
`irs990_filing_*` schedule tables. `organizations` holds the stable EIN-level
identity, while `irs990_source_objects` records the immutable IRS filename,
fingerprint, parser version, and import state. This preserves multiple returns
for one EIN and lets bulk imports resume without reparsing completed files.

`irs990_filings` stores the filer mailing address in `filer_address`,
`filer_city`, `filer_state`, and `filer_zip_code`. Address fields are populated
when the source XML is ingested; updating the parser does not backfill existing
rows that were already marked successful. Re-run ingestion for the source XML
with the current parser version when an existing database needs the address
fields refreshed. The filing-scoped `doing_business_as_name` field preserves
the optional DBA name separately from the legal `filer_name`.

The parser version is stored per source object. A parser-version change makes
the next `irs990` directory or ZIP ingestion reparse previously successful XML
files, so newly added fields such as `program_services_amt` and
`management_and_general_amt` are populated without changing the source files.

The older `orgs` and `org_*` tables remain for compatibility with early
notebooks, but should not be used for multi-year IRS analysis. Use
`irs990_filings` as the starting point, then join schedule tables on
`filing_id`.

IRS, FEC, and LDA do not share a universal ID. `extract.entities` normalizes
names and persists exact normalized-name results as reviewable candidates in
`entity_match_candidates`; they are not automatic identity assertions. Accept
or reject candidates in `entity_match_decisions` before using them as joins.

## Project workflow

Use one SQLite file per research dataset. Load IRS, FEC, LDA, and Congress data
into that same file by passing `db_path=...` to each collector, then run
`extract.pipeline.refresh_analysis_layers`. The refresh creates reviewable
name-match candidates, extracts explicit lobbying bill references, and builds
views for grants, related organizations, approved external links, and
organization-to-bill facts.

`notebooks/01_build_dataset.ipynb` is the loading workflow and
`notebooks/02_review_and_explore.ipynb` is the review/analysis workflow. They
intentionally import reusable Python functions rather than carrying their own
ETL or matching logic. Cross-source names become analysis joins only after a
reviewer records an `accepted` decision.

## Transparency index

The transparency pipeline produces a filing-year score based on the nine-component
transparency index (`irvin-9-v1`). The board component uses the total
governing-body count from Form 990 Part I, line 4. Missing volunteer,
relationship, political-expense, salary, and unrestricted-asset values are
treated as zero inputs. A filing with no usable website receives a zero-word
website score; related-site observations are still honored, while a usable
submitted website without a successful observation stays missing. Ratio
components still stay missing when their denominator is unavailable, except
for fundraising when both expense and grant values indicate no activity. Each
score records its component version.
The notebook documents the formulas and limitations.
Website observations are cached in SQLite with the crawl policy, timestamps,
status, page URLs, and capped word counts.

Run a small crawl first:

```bash
python -m extract.run transparency-index --db data/irs990_full.db \
  --max-sites 25 --max-pages 10
```

The source snapshot and scores are written to SQLite. Add `--export-parquet`
to also write the current Parquet representation and JSON manifest. Use
`--no-crawl` to score only cached observations, or `--max-sites 0` for a full
crawl. Crawl runs persist their candidate snapshot and commit each URL
independently, so an interrupted run can be resumed:

```bash
python -m extract.run transparency-index --db data/irs990_full.db \
  --max-sites 0 --max-pages 10 --timeout 15 --delay 0.25 --workers 4

python -m extract.run transparency-index --db data/irs990_full.db \
  --resume
```

The second command resumes the latest running or interrupted crawl. Pass a
specific run ID to `--resume` when more than one resumable run exists. Network
crawls may use a small bounded worker count; SQLite writes remain serialized
and each completed URL is checkpointed. The score table is also persisted in
SQLite for downstream modeling. Export a stored SQL run with:

```bash
python -m extract.run --db data/irs990_full.db \
  transparency-export-parquet --run-id RUN_ID
```

Import the current Parquet pair into the SQL tables with:

```bash
python -m extract.run --db data/irs990_full.db \
  transparency-import-parquet \
  --scores data/transparency_index/transparency_index__RUN_ID.parquet \
  --source data/transparency_index/transparency_source__RUN_ID.parquet
```

Refresh available IRS source objects after parser changes:

```bash
python -m extract.run --db data/irs990_full.db reingest-irs990 \
  --root . --path-prefix drive/ --eligible-only --force
```

`load_modeling_features()` defaults to complete rows for modeling. Use
`min_observed_components` when a lower coverage threshold is justified.

## End-to-end Transparency Index notebook

`notebooks/transparency_index.ipynb` is the canonical end-to-end workflow. It
documents the nine components and their limitations, checks complete and
thresholded populations, explores coverage and score distributions, evaluates
a grouped disclosed-activity classifier, summarizes the organization network,
and reviews clustering and anomaly candidates. It keeps component missingness
separate from Schedule R relationship prevalence and explains how to use each
model output.

`notebooks/03_modeling.ipynb` remains as a compatibility copy of the modeling
workflow; new analysis should use `transparency_index.ipynb`.

Run it from the repository root with:

```bash
python -m jupyter nbconvert --to notebook --execute \
  notebooks/transparency_index.ipynb --output /tmp/transparency_index_executed.ipynb
```
