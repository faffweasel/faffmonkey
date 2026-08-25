# Self-Review Entry Templates

Load this file when writing entries manually. Not needed when using add.py.

## Entry Format

All entry types (LRN, ERR, FEAT) use the same format in `workspace/LEARNINGS.md`:

```markdown
## [TAG-YYYYMMDD-NNN] label
**Status**: pending
**Priority**: low | medium | high | critical
**Area**: backend | infra | tests | docs | config
**Summary**: One-line description
**Details**: Full context (optional, can be multi-line)
```

## ID Format

`TAG-YYYYMMDD-NNN`
- TAG: `LRN` (learning/correction), `ERR` (error/failure), `FEAT` (feature gap)
- YYYYMMDD: Date in UTC
- NNN: Sequential number within the day per tag (001, 002, ...)

## Tags

| Tag | Label | Use for |
|-----|-------|---------|
| LRN | learning | Corrections, knowledge gaps, best practices discovered |
| ERR | error | Command failures, API errors, tool breakage |
| FEAT | feature | Missing capabilities, user requests |

## Statuses

| Status | Meaning |
|--------|---------|
| pending | New, not yet acted on |
| promoted | Distilled and added to AGENTS.md or MEMORY.md |

## Priority Levels

| Priority | When to use |
|----------|-------------|
| critical | Blocks core functionality, data loss risk, security issue |
| high | Significant impact, affects common workflows, recurring issue |
| medium | Moderate impact, workaround exists (default) |
| low | Minor inconvenience, edge case |

## Area Tags

| Area | Scope |
|------|-------|
| backend | API, services, server-side code |
| infra | CI/CD, deployment, Docker, cloud |
| tests | Test files, testing utilities |
| docs | Documentation, READMEs |
| config | Configuration, environment, settings |
