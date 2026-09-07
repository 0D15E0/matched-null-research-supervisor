# Documentation

This folder explains how the local research supervisor works, how it drives
the `cli_trader` backtester, and what every status in its ledger means. The
operational manual is one level up in [../RUNBOOK.md](../RUNBOOK.md); the
design history is in [../LOCAL_OLLAMA_RESEARCH_PLAN.md](../LOCAL_OLLAMA_RESEARCH_PLAN.md).

Read in this order:

| document | answers |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | What is this, what problem does it solve, what will it never do |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The parts, the files on disk, the process model, provenance |
| [THE_LOOP.md](THE_LOOP.md) | One iteration end to end: schedule, propose, validate, evaluate, classify, review |
| [CLI_INTERFACE.md](CLI_INTERFACE.md) | Exactly how the supervisor talks to `cli_trader`, and the boundary it never crosses |
| [RULE_LANGUAGE.md](RULE_LANGUAGE.md) | The `generated_spec` rule language the model composes new strategies in |
| [PROMPTS_AND_MODELS.md](PROMPTS_AND_MODELS.md) | What the generator and reviewer are told, which local models run them, and why |
| [FINDINGS.md](FINDINGS.md) | What the loop has actually found, and how far each result fell under scrutiny |
| [GLOSSARY.md](GLOSSARY.md) | Every status, classification, event type and term |

Diagrams are Mermaid blocks. GitHub renders them inline; in VS Code use the
Markdown preview with the "Markdown Preview Mermaid Support" extension, or
paste a block into <https://mermaid.live>. The two diagrams that matter most
also have a plain-text version next to them.

All numbers quoted in these documents are from the code and ledger as of
2026-09-05. Where a number is a design choice rather than a measurement, the
document says so.
