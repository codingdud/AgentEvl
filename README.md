# Agent Test Harness

UV-based Python harness for testing agents, skills, and prompts.
All agent, skill, and prompt files live under `.github/` — the harness reads them from there.

## Structure

```
promtEvl/
├── .github/
│   ├── agents/            ← drop *.agent.md files here
│   ├── skills/            ← drop skill folders (with SKILL.md) here
│   └── prompts/           ← drop *.prompt.md files here
├── harness/
│   └── runner.py          # loads scenarios, calls LLM, scores, saves results
├── scenarios/
│   ├── agents/agents.yaml
│   ├── skills/skills.yaml
│   ├── prompts/prompts.yaml
│   └── mcps/mcps.yaml
├── results/               # JSON run outputs (gitignored)
├── .env.example
└── pyproject.toml
```

## Setup

```bash
cp .env.example .env        # fill in OPENROUTER_API_KEY
uv sync
```

## Run

```bash
# all scenarios
uv run python -m harness.runner

# one category
uv run python -m harness.runner --category agents
uv run python -m harness.runner --category skills
uv run python -m harness.runner --category prompts
uv run python -m harness.runner --category mcps

# single scenario by id
uv run python -m harness.runner --id coding-agent-minimal-change

# list all discovered .github/ files
uv run python -m harness.runner --list

# skip saving results
uv run python -m harness.runner --no-save
```

## Adding agents / skills / prompts

1. Drop the file into the matching `.github/` subfolder:
   - agents → `.github/agents/my-agent.agent.md`
   - skills → `.github/skills/my-skill/SKILL.md`
   - prompts → `.github/prompts/my-prompt.prompt.md`

2. Add a scenario entry in the matching `scenarios/<category>/*.yaml` file.
   Set `system:` to the path **relative to `.github/`**, e.g.:
   ```yaml
   system: "agents/my-agent.agent.md"
   ```

3. Run `uv run python -m harness.runner --id your-new-id` to validate in isolation.

## Scenario YAML format

```yaml
- id: my-scenario-slug          # unique, kebab-case
  category: agents              # agents | skills | prompts | mcps
  target: "AgentName"
  description: "What capability is under test"
  system: "agents/my-agent.agent.md"   # relative to .github/, or inline text
  user: |
    Do X given Y.
  checks:
    - type: contains            # case-insensitive substring match
      value: "expected phrase"
    - type: not_contains        # must NOT appear
      value: "bad phrase"
    - type: llm_judge           # LLM scores pass/fail against a criterion
      value: "The response does X and does not do Y."
  model: openrouter/anthropic/claude-3-5-haiku   # optional override
  tags: [smoke, regression]     # optional
```

## Prompt optimization (DSPy + openevals)

The harness includes a DSPy-based optimizer that rewrites agent/skill/prompt instructions
to improve scores, using openevals as the judge metric.

```bash
# baseline eval only (no changes written)
uv run python -m harness.optimizer --scenario-id coding-agent-minimal-change

# eval + optimize with MIPROv2 (rewrites instructions)
uv run python -m harness.optimizer --scenario-id coding-agent-minimal-change --optimize

# eval + optimize with BootstrapFewShot (adds few-shot examples instead)
uv run python -m harness.optimizer --scenario-id coding-agent-minimal-change --optimize --optimizer bootstrap
```

Outputs:
- `results/<scenario-id>.optimized.json` — full DSPy program state (loadable with `dspy.load()`)
- `.github/agents/<agent>.optimized.md` — rewritten instructions alongside the original

### Provider config

Set `PROVIDER` in `.env` to switch between backends:

| PROVIDER | Key needed | Default model |
|----------|-----------|---------------|
| `openrouter` | `OPENROUTER_API_KEY` | `openrouter/anthropic/claude-3-5-haiku` |
| `gemini` | `GEMINI_API_KEY` | `gemini/gemini-1.5-flash` |
| `openai` | `OPENAI_API_KEY` | `openai/gpt-4o-mini` |

Override the model with `MODEL=<litellm-slug>` in `.env`.

## Check types

| type | behaviour |
|------|-----------|
| `contains` | passes if `value` appears (case-insensitive) in the LLM output |
| `not_contains` | passes if `value` does NOT appear |
| `llm_judge` | sends output + criterion to the LLM; passes if verdict contains "PASS" |
