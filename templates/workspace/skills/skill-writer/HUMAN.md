# skill-writer: setup and notes

Lets the agent scaffold, validate, and package new skills. No setup
required.

## Use

Ask the agent to "make a skill for X" or "save this as a skill". It
scaffolds `workspace/skills/<name>/` with SKILL.md and HUMAN.md
templates, fills them in, and validates the result. Always review the
generated files before relying on them; the agent is told to remind you.

## How skills are organised

- `workspace/skills/`: your live skills, loaded at session start (each
  skill's description goes into the system prompt; the body is loaded
  only on invocation).
- Built-ins ship with faffmonkey and are installed by `faff init`;
  optional capabilities live in the repo's `contrib/skills/` and are
  installed with `faff skill install <name>`.
- Skill data (caches, indexes, queues) lives in
  `workspace/skills-data/<name>/`, kept out of the skill directory so
  skills stay copyable.

## Packaging

`package_skill` produces a `.skill` zip for sharing a skill with someone
else. It validates first and skips symlinks, `.git`, `__pycache__`, and
`node_modules`. To install a received `.skill` file, unzip it into
`workspace/skills/` and review its contents before the next session
picks it up.
