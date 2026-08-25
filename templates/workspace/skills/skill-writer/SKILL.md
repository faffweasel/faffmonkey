---
name: skill-writer
description: Create a new skill when the user asks for one, or when you identify a repeated task that should become a reusable automation. Also use when the user says 'save this as a skill' or 'make a skill for this'. Creates the full directory structure and a SKILL.md template. Always tell the user to review the generated files.
actions: init_skill, quick_validate, package_skill
---

## Core principles

**The context window is a public good.** Skills share it with the system prompt, conversation history, other skills' metadata, and the user's request. Only add what the agent doesn't already know. Challenge every paragraph: does this justify its token cost?

**Match specificity to fragility.** High freedom (prose instructions) when multiple approaches work. Medium freedom (pseudocode, parameterised scripts) when a preferred pattern exists. Low freedom (deterministic scripts, few parameters) when operations are fragile and consistency is critical. Think of it as a path: a narrow bridge needs guardrails, an open field doesn't.

**SKILL.md is for the agent, HUMAN.md is for the human.** SKILL.md contains runtime instructions only: when to invoke, actions, how to interpret output. Setup steps, API keys, configuration, architecture explanations, and maintenance notes go in HUMAN.md, which is never loaded into context. Every skill gets both. No other auxiliary documentation (README.md, INSTALLATION_GUIDE.md, CHANGELOG.md): a skill contains what the agent needs to do the job, plus HUMAN.md for its human.

## Anatomy of a skill

```
skill-name/
  SKILL.md          <- required: YAML frontmatter + Markdown body, agent-facing
  HUMAN.md          <- human-facing: setup, configuration, maintenance
  scripts/          <- optional: executable code (Python/Bash)
  references/       <- optional: docs loaded on demand via file_read
  assets/           <- optional: templates, data files used in output
```

Companion data directory (indexes, caches, queues): `workspace/skills-data/<name>/`

### Frontmatter

| Field | Required | Notes |
|---|---|---|
| `name` | Yes | Lowercase kebab-case, max 64 chars. Must match directory name. |
| `description` | Yes | 1-1024 chars. Appears in every system prompt (Tier 1). Must be good enough for the agent to decide whether to invoke. |
| `actions` | No | Comma-separated list of available script actions. Each must match a script filename in scripts/ exactly (`decay` runs `decay.py`; hyphens are not translated to underscores). |
| `metadata` | No | Single-line JSON. Use `metadata: {"faffmonkey": {"requires": {"env": ["KEY"], "bins": ["ffmpeg"]}}}` for load-time gating. |
| `timeout` | No | Seconds the harness allows an action to run (default 60, capped at 600). Declare it when an action calls a slow external API, and leave headroom above any subprocess timeout inside the script. |

### Body conventions

Five required sections: **When to use**, **What it does**, **Arguments and flags**, **Examples**, **Limitations**. Max ~500 lines. Content beyond that belongs in `references/`. Sections can merge when the skill is simple (arguments folded into action descriptions, examples inline), but the content of all five must be present.

### Script naming rules

- A skill meant to run from a `session: "none"` cron job MUST name its entry script `run.py` (or `run`); that is the only script a none-session job executes.
- Never name a script after a Python stdlib module (`calendar.py`, `email.py`): the script directory is sys.path[0] in its own subprocess, so the script shadows the stdlib module for itself.

### When to use scripts/ vs references/ vs assets/

**scripts/** when determinism matters: the same code keeps getting rewritten, operations are fragile, or you need guaranteed output format. Scripts run via subprocess; they can be executed without loading into context.

**references/** for on-demand documentation: API schemas, domain knowledge, detailed guides. The agent loads these via file_read only when it decides it needs them. If a reference exceeds ~10K words, include grep patterns in SKILL.md so the agent can search it.

**assets/** for output templates and data files not loaded into context: slide templates, font files, boilerplate project directories, sample data. The agent copies or uses these in its output.

Not every skill needs all three. Most need only scripts/.

## Structural patterns

Choose the pattern that fits, then fill in the five body sections:

**Workflow-based** (sequential processes): init -> configure -> validate -> deploy. Best when there are clear step-by-step procedures.

**Task-based** (independent operations): list, add, remove, search. Best when the skill offers different operations. Most faffmonkey skills use this.

**Reference/guidelines** (standards, specs): brand colours, API schema, coding rules. Best for knowledge the agent should follow.

**Capabilities-based** (interrelated features): auth + sessions + tokens. Best when features are tightly coupled.

Patterns can mix. Start with task-based, add workflow elements for complex operations.

## Description-writing checklist

The description is the primary trigger mechanism. The agent sees it in every system prompt and decides whether to invoke based on it alone.

1. **Front-load capability keywords.** "Schedule, list, and manage recurring tasks" not "A tool for managing tasks."
2. **Include both what and when.** "Use when the user says 'every morning', 'remind me', 'check daily'."
3. **Trigger-based, not implementation-based.** Describe what the user says, not how the code works.
4. **Slightly pushy.** "Also use when you identify a repeated task that should become reusable automation" encourages the agent to invoke proactively.
5. **Negative boundaries.** "Do NOT use for running jobs manually. Use `faff cron run` for that."
6. **Under 1024 characters.** The bootstrap loads every skill's description. Keep them tight.

## Process

Three scripts map to the creation workflow:

### 1. init_skill.py: scaffold

Creates the directory structure, SKILL.md and HUMAN.md templates, and the skills-data directory.

```
init_skill my-new-skill
init_skill my-api-helper --resources scripts,references
init_skill my-tool --resources scripts --examples
init_skill test-skill --path /tmp/test-skills
```

Default output: `workspace/skills/<name>/`. Override with `--path` for testing. Name is normalized to kebab-case.

After init, fill in all TODO sections in both SKILL.md and HUMAN.md, update the description, and set the actions field.

### 2. quick_validate.py: check

Validates frontmatter format, allowed fields, name conventions, and description quality.

```
quick_validate my-new-skill
```

Checks: name is kebab-case without consecutive hyphens (max 64 chars), description under 1024 chars with no angle brackets, no unexpected frontmatter keys, and warns if description lacks trigger language ("Use when..." or "Use for...").

### 3. package_skill.py: distribute (optional)

Packages a skill into a `.skill` zip file for sharing. Validates first, then bundles all files while skipping symlinks, `.git`, `__pycache__`, and `node_modules`. Rejects files that resolve outside the skill root (path traversal prevention).

```
package_skill my-new-skill
```

## Skill extraction from learnings

When self-review identifies a recurring pattern worth automating, skill-writer creates the skill. Extraction criteria:

- **Recurring:** 3+ similar learnings about the same task or workflow.
- **Verified:** the learnings have status resolved (the approach works).
- **Non-obvious:** the pattern requires domain knowledge the agent wouldn't have.
- **Broadly applicable:** the pattern applies across conversations, not just one context.
- **User-flagged:** the user explicitly says "save this as a skill" or "make a skill for this".

When extracting: the learnings become the seed content for the SKILL.md body. The recurring corrections become the "Limitations" or "When to use" contrast blocks.

## Cross-skill references

When two skills have overlapping triggers (e.g. cron-manager and a reminders skill), add disambiguation blocks in both:

- In descriptions: "Use cron-manager for precise scheduled tasks. Use reminders for casual 'remind me' requests without specific times."
- In "When to use" sections: explicit contrast blocks explaining when to choose one over the other.

Document the overlap. Silent collisions waste context on double-loading and confuse the agent.
