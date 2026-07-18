# Test fixtures

Reference-JSON fixtures used by `ref-checker`'s tests. All fixtures are
committed as bare JSON arrays conforming to
[`ref_checker/skills/reference-checking/references/schema.md`](../../ref_checker/skills/reference-checking/references/schema.md).

Fixtures under `refs/` are treated as **golden data**: extract-once, commit,
and reuse across many test runs. Do not regenerate on every test run.

## Provenance

Five fixtures were extracted from publicly-available redistributable papers
hosted in the [pub-analysis](https://github.com/rbross-hpc/pub-analysis)
sibling repository (BSD-3-Clause). Each source PDF is CC-BY 4.0 or U.S.
Federal public domain; the extracted reference lists inherit no additional
restrictions.

For each fixture below: source PDF URL, DOI, OSTI ID, license, and the
`ref-checker extract` command used to produce it.

### `refs/klasky_5.json` (13 references)

- **Title:** Scalable foundation models for numerical simulations on HPC platforms
- **Authors:** Dali Wang, Qian Gong, Zirui Liu, Xiao Wang, Qinglei Cao, Scott Klasky
- **Venue:** Frontiers in High Performance Computing (2026)
- **DOI:** [10.3389/fhpcp.2026.1778471](https://doi.org/10.3389/fhpcp.2026.1778471)
- **OSTI ID:** 3028571
- **License:** CC-BY 4.0
- **Source URL:** https://www.osti.gov/servlets/purl/3028571

### `refs/zfp_spectral.json` (13 references)

- **Title:** Supporting Special Values in ZFP
- **Authors:** Peter Lindstrom (LLNL)
- **Venue:** OSTI white paper / technical report
- **DOI:** [10.2172/2998448](https://doi.org/10.2172/2998448)
- **OSTI ID:** 2998448
- **License:** Public domain (U.S. Government work, 17 U.S.C. § 105)
- **Source URL:** https://www.osti.gov/servlets/purl/2998448

### `refs/dorier_mofka.json` (64 references)

- **Title:** Toward a persistent event-streaming system for HPC applications
- **Authors:** Matthieu Dorier, Amal Gueroudji, Valérie Hayot-Sasson, et al.
- **Venue:** Frontiers in High Performance Computing (2025)
- **DOI:** [10.3389/fhpcp.2025.1638203](https://doi.org/10.3389/fhpcp.2025.1638203)
- **OSTI ID:** 3002321
- **License:** CC-BY 4.0
- **Source URL:** https://www.osti.gov/servlets/purl/3002321
- **Use:** The "big" fixture — 64 refs push more sources per session.

### `refs/cruz_zombie.json` (24 references)

- **Title:** Hybrid PDES Simulation of HPC Networks Using Zombie Packets
- **Authors:** Elkin Cruz-Camacho, Kevin A. Brown, Xin Wang, et al.
- **Venue:** ACM TOMACS (2025), vol. 35 no. 2
- **DOI:** [10.1145/3682060](https://doi.org/10.1145/3682060)
- **OSTI ID:** 3017061
- **License:** Federally-funded author manuscript (DOE grant AC02-06CH11357)
- **Source URL:** https://www.osti.gov/servlets/purl/3017061

### `refs/wan_e3smv2.json` (57 references)

- **Title:** Features of mid- and high-latitude low-level clouds and their
  relation to strong aerosol effects in E3SMv2
- **Authors:** Hui Wan, Abhishek Yenpure, Berk Geveci, Richard C. Easter,
  Philip J. Rasch, Kai Zhang, Xubin Zeng
- **Venue:** Geoscientific Model Development (2025), vol. 18 no. 17
- **DOI:** [10.5194/gmd-18-5655-2025](https://doi.org/10.5194/gmd-18-5655-2025)
- **OSTI ID:** 2587778
- **License:** CC-BY 4.0
- **Source URL:** https://www.osti.gov/servlets/purl/2587778

### `refs/mixed_small.json` (10 references, hand-crafted)

Curated smoke-test set exercising every lookup mode: DOI, arXiv ID,
title-only, GitHub URL, generic URL, deliberately-unfindable ref. No PDF
provenance — the reference metadata is drawn from well-known public papers
(e.g. Attention is All You Need, BERT, XGBoost, Adam, scikit-learn) plus
one entry that should always miss (`title: "A Ref That Should Not Be
Findable Anywhere"`).

### `refs/edge_cases.json` (5 references, hand-crafted)

Deliberate edge cases for defensive-coding tests:

1. All fields null (title, DOI, arxiv_id) — should NO_MATCH with no HTTP calls.
2. DOI-only, no arxiv_id.
3. arXiv-ID-only.
4. GitHub URL only (dataset-style ref).
5. Title + year only (no identifiers) — forces title-search on all sources.

## Regenerating (rarely needed)

Regenerate only when the `ref_checker/extract.py` schema changes in a way
that requires a matching update to the golden fixtures. In that case:

```bash
# Download source PDFs listed above into a scratch directory.
mkdir -p /tmp/pubs
cd /tmp/pubs
curl -sL -o klasky-5.pdf     https://www.osti.gov/servlets/purl/3028571
curl -sL -o zfp-spectral.pdf https://www.osti.gov/servlets/purl/2998448
curl -sL -o dorier-mofka.pdf https://www.osti.gov/servlets/purl/3002321
curl -sL -o cruz-zombie.pdf  https://www.osti.gov/servlets/purl/3017061
curl -sL -o wan-e3smv2.pdf   https://www.osti.gov/servlets/purl/2587778

# Extract references via LLM (requires OPENAI_API_KEY).
for f in *.pdf; do ref-checker extract "$f"; done

# Copy the .refs.json files (stripping the sidecar wrapper) into
# tests/fixtures/refs/, renaming with underscores. The wrapper strip is:
#   jq '.references' <in>.refs.json > <out>.json
```
