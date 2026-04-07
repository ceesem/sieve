# Sieve

**Automated literature monitoring for researchers.**

Sieve fetches new papers daily from bioRxiv, arXiv, and journal RSS feeds, scores them against your research interests using Claude, and presents the results as a local web interface. You can also walk the citation graph of any paper to catch up on related literature.

---

## How it works

1. **Fetch** — pulls new papers from configured sources (bioRxiv, arXiv, RSS feeds)
2. **Score** — runs a two-stage Claude pipeline (Haiku triage → Sonnet scoring) against your `interests.md` profile
3. **Ingest** — stores papers and scores in a local SQLite database
4. **Serve** — generates a static site you can browse and annotate locally

Papers are scored 1–10. You configure thresholds for what gets stored and what appears in the UI.

---

## Requirements

- [uv](https://docs.astral.sh/uv/) — Python package manager
- [Claude Code CLI](https://claude.ai/code) — must be installed and authenticated (`claude -p` must work)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yourname/sieve.git
cd sieve
uv sync
```

### 2. Configure

```bash
cp config/settings.yaml.example config/settings.yaml
cp config/interests.md.example config/interests.md
```

Edit **`config/settings.yaml`** — set your sources, thresholds, and email:

```yaml
lookback_days: 2

store_threshold: 5
display_threshold: 7
site_threshold: 4

batch_size: 30          # papers per Haiku triage batch
sonnet_batch_size: 40   # papers per Sonnet scoring batch

biorxiv_category: neuroscience

arxiv_categories:
  - q-bio.NC

mailto: "you@example.com"  # used for OpenAlex polite pool

max_papers_per_source: 500

feeds:
  - name: "Nature Neuroscience"
    url: "https://www.nature.com/neuro.rss"
  - name: "Nature"
    url: "https://www.nature.com/nature.rss"
```

Edit **`config/interests.md`** — describe what you want to read. This is the prompt fed to Claude for scoring. Be specific: name topics, methods, labs, and things you explicitly don't want. The quality of scoring depends directly on this file.

<details>
<summary>Example interests.md</summary>

```markdown
## Research Interests

### Core topics

- Visual cortex circuits in mice — response properties, layer and cell-type specificity, feedforward and feedback pathways
- How interneuron subtypes (PV, SST, VIP) shape cortical gain, selectivity, and network dynamics
- Population coding and dimensionality in visual cortex during perception and behavior
- Top-down modulation of sensory cortex — attention, locomotion, arousal state effects on V1/HVA responses

### Methods I follow

- Two-photon calcium imaging and large-scale Neuropixels recordings in behaving mice
- Optogenetic dissection of specific cell types or pathways during visual tasks
- Computational models of visual cortical circuits

### Explicitly NOT interested in

- Human or primate visual neuroscience unless methods are directly transferable to mice
- MRI or EEG-based studies
- Clinical or disease contexts
- Pure psychophysics without a neural circuit component
```

</details>

### 3. Run

```bash
sieve run       # fetch → score → ingest → generate site
sieve serve     # open the site in your browser
```

---

## Daily use

| Command | Description |
|---------|-------------|
| `sieve run [--site-threshold N]` | Fetch new papers, score, and update the site |
| `sieve serve` | Start local server and open browser |
| `sieve seed --doi <DOI> [--pdf PATH]` | Evaluate a paper and optionally update your interests profile |
| `sieve cite --doi <DOI> [--forward] [--recommend] [--site-threshold N]` | Score the citation graph of a paper |
| `sieve clean` | Prune low-score papers outside the fetch window |
| `sieve export --from FILE [--output PATH] [--title TEXT] [--interests PATH]` | Generate a standalone annotated bibliography HTML |

`--site-threshold N` overrides the `site_threshold` from settings for that run (useful for one-off exploration without changing your config). `--pdf PATH` on `seed` lets you supply a local PDF when the DOI fetch doesn't yield a full abstract.

### Citation graph (`sieve cite`)

Use this to catch up on a paper's references — useful when you encounter a key paper and want to know which of its citations you should read.

```bash
# References of a paper (backward citations)
sieve cite --doi 10.1101/2022.09.29.510081

# Also fetch papers that cite this paper (forward citations)
sieve cite --doi 10.1101/2022.09.29.510081 --forward

# Include S2-computed related papers
sieve cite --doi 10.1101/2022.09.29.510081 --recommend
```

Accepts DOIs, bioRxiv DOIs, Semantic Scholar paper IDs, Corpus IDs, or full Semantic Scholar URLs:

```bash
sieve cite --doi 252528267
sieve cite --doi "https://www.semanticscholar.org/paper/Title/f583cb7b6e6aa669..."
```

> **Note:** Many journal papers are indexed in Semantic Scholar under their preprint DOI. If a journal DOI fails, try the bioRxiv DOI. Sieve falls back to OpenAlex automatically when Semantic Scholar blocks reference access (common for Elsevier papers).

### Seeding your interests (`sieve seed`)

When you read a paper that made you realise your interests profile is missing something, run:

```bash
sieve seed --doi 10.1038/s41593-022-01107-4
```

Claude evaluates whether the paper represents a gap in your `interests.md` and suggests an addition. You confirm before anything is written.

### Annotated bibliography (`sieve export`)

Generate a standalone HTML bibliography from a list of DOIs — useful for sharing a curated reading list or preparing a literature review.

```bash
# From a plain-text file (one DOI per line, # comments ignored)
sieve export --from dois.txt

# From a BibTeX file
sieve export --from refs.bib --output report/bibliography.html --title "My Reading List"

# Re-annotate with a custom interests profile (re-scores via Sonnet; does not modify DB)
sieve export --from refs.bib --interests config/interests_project.md
```

Papers must already be in your local database (run `sieve cite` or `sieve run` first to ingest them). DOIs missing from the DB are reported but don't abort the export.

For BibTeX input, `sieve export` requires every entry to have a `doi` field. If any are missing it will prompt before continuing — pass `--ignore-errors` to skip the prompt and proceed anyway.

Output defaults to `site/bibliography.html`.

---

## Scoring thresholds

Configured in `settings.yaml`:

Scoring uses a two-stage Claude pipeline: Haiku for fast triage, then Sonnet for final scores on papers that pass triage.

| Setting | Default | Meaning |
|---------|---------|---------|
| `store_threshold` | 5 | Minimum score to save to DB |
| `display_threshold` | 7 | Minimum score shown highlighted in UI |
| `site_threshold` | 4 | Minimum score shown in site at all |
| `lookback_days` | 2 | Days of papers shown in site |
| `batch_size` | 30 | Papers per Haiku triage batch |
| `sonnet_batch_size` | 40 | Papers per Sonnet scoring batch |
| `max_papers_per_source` | 500 | Max papers fetched per source per run |

---

## Scheduled runs (macOS)

`setup_launchd.sh` installs a launchd job that runs `sieve run` daily:

```bash
bash setup_launchd.sh                      # runs at 6:00 AM daily
bash setup_launchd.sh --hour 8 --minute 30
launchctl start com.sieve.run              # trigger immediately to test
bash setup_launchd.sh --uninstall
```

Logs are written to `data/logs/launchd.log`.

---

## Optional: Semantic Scholar API key

The public S2 API works without a key at low request rates. If you hit rate limits, get a free key at [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) and set:

```bash
export S2_API_KEY=your_key_here
```

---

## License

MIT — see LICENSE file for details.
