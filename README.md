# Master's Thesis — GStreamer RTP Video Pipeline

## Repository Layout

```
Masterthesis/
├── Code/                   # All Python scripts
│   ├── Analyzers/          # Post-run analysis and graphs
│   │   └── graphs/         # Generated output graphs
│   ├── logs/               # Runtime log files (CSV)
│   └── oldCodeAndLogs/     # Legacy scripts and old test data
├── Sections/               # LaTeX thesis chapters
├── Appendices/             # LaTeX appendices
├── Images/                 # Figures used in the thesis
├── out/                    # Compiled LaTeX output (PDF, aux files)
├── main.tex                # LaTeX entry point
├── references.bib          # Bibliography
├── setup_guide.md          # System setup and installation
└── testing_guide.md        # How to run sender/receiver pairs
```

---

## Code

All sender, receiver, and router scripts are in [`Code/`](Code/).

- Senders are named `senderV22-*.py`
- Receivers are named `receiverV14-*.py` / `receiverV15-*.py`
- Minimal/debug variants: `sender-Pure.py`, `receiver-Pure.py`, `sender-minimal-logged.py`

See [testing_guide.md](testing_guide.md) for which sender and receiver to pair, and in what order to start them.

## Logs and Graphs

**Log files** are written to [`Code/logs/`](Code/logs/) during a test run, organized by type:

| Folder | Contents |
|---|---|
| `logs/pipeline/sender/` | Sender pipeline latency (CSV) |
| `logs/pipeline/receiver/` | Receiver pipeline latency (CSV) |
| `logs/transit/` | Network transit times (CSV) |
| `logs/quality/` | BRISQUE video quality scores (CSV) |
| `logs/throughput/` | Bandwidth usage (CSV) |
| `logs/cpu/` | CPU utilization (log) |

**Analysis scripts** live in [`Code/Analyzers/`](Code/Analyzers/). Run `python3 run_all_analyses.py` there after a test to process logs and produce graphs.

**Generated graphs** are saved to [`Code/Analyzers/graphs/`](Code/Analyzers/graphs/), grouped by run timestamp.

## Old Code and Logs

Earlier script versions and archived test data are in [`Code/oldCodeAndLogs/`](Code/oldCodeAndLogs/).

## Thesis Text and PDF

LaTeX source files are in [`Sections/`](Sections/) (chapters) and [`Appendices/`](Appendices/).

The compiled PDF is at [`out/main.pdf`](out/main.pdf). To recompile, run `latexmk` from the repo root (requires a LaTeX distribution with `latexmk`).

---

## Guides

- [setup_guide.md](setup_guide.md) — Install GStreamer, Python bindings, Tailscale, and configure both machines
- [testing_guide.md](testing_guide.md) — Run sender/receiver pairs, collect logs, and analyze results
