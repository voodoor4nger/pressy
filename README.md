# Pressy

A self-hosted dashboard that tracks the performance of the current US
presidential administration across ten policy and governance categories,
updated by curated news events from across the political spectrum.

Pressy is for personal clarity, not publication. It uses Gemini to
extract structured event data from articles spanning the political
spectrum (AllSides ratings as canonical source classification) and
rolls them up into per-category scores.

## Categories tracked
Economy, Job market, Housing, Health, Education, Science and technology,
International relations, Constitutional stewardship, Moral leadership,
Institutional durability.

## Setup

```bash
git clone <repo-url> pressy
cd pressy

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# edit .env and set GEMINI_API_KEY to your own key
```

## Run the pipeline

End-to-end: pulls every RSS feed listed in `config/sources.yaml`,
filters articles by relevance, fetches and dedupes bodies, then runs
Gemini extraction on each new article and persists the events.

```bash
python -m src.pipeline
```

The first run creates `data/pressy.db`. Subsequent runs only ingest
new articles (URL- and content-hash-deduplicated). The pipeline
rate-limits Gemini calls to one every 5 seconds by default; expect
a full run to take a few minutes.

## Run a test extraction (no DB writes)

Pipes hardcoded sample articles through the LLM and prints the
extracted JSON. Useful for tuning the prompt without polluting the
database:

```bash
python -m src.extract
```

## Run the test suite

```bash
pytest tests/
```

The extraction test is skipped automatically if `GEMINI_API_KEY` is not set.

## Project layout

```
pressy/
├── src/
│   ├── llm.py         # GeminiClient (rate-limited, JSON-mode)
│   ├── extract.py     # event extraction from articles
│   ├── ingest.py      # RSS ingestion + body fetch + relevance filter
│   ├── db.py          # SQLite schema + helpers
│   ├── pipeline.py    # ingest + extract orchestrator
│   ├── cluster.py     # event deduplication (TBD)
│   └── score.py       # category scoring rollup (TBD)
├── prompts/           # LLM prompts
├── config/            # source list (sources.yaml)
├── data/              # local SQLite (gitignored)
└── tests/             # pytest suite
```

See `CLAUDE.md` for the project's analytical and methodological norms.
