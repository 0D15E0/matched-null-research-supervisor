# Agent Roles

The local research supervisor separates responsibilities:

- `SUPERVISOR.md`: deterministic orchestrator and policy enforcement;
- `GENERATOR.md`: proposes one structured hypothesis;
- `REVIEWER.md`: annotates deterministic evidence after evaluation.

Only the supervisor can schedule experiments. The generator and reviewer never
execute commands, access holdout data, edit deployment files, or override
protocol classifications.
