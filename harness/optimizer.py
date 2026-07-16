"""
harness/optimizer.py

DSPy-based prompt optimizer. openevals supplies the judge metric inside DSPy.

Architecture:
  - DSPy drives the loop: evaluate → optimize (rewrite instructions / add few-shot examples)
  - openevals.create_llm_as_judge is the scoring function plugged into DSPy's metric
  - All three providers (OpenRouter, Gemini, OpenAI) are supported via litellm

Verified against:
  dspy==3.2.1   openevals==0.2.0   litellm==1.66.3

Run:
  uv run python -m harness.optimizer --scenario-id <id>
  uv run python -m harness.optimizer --scenario-id <id> --optimize
  uv run python -m harness.optimizer --scenario-id <id> --optimize --optimizer bootstrap

Provider selection (set in .env):
  PROVIDER=openrouter  →  OPENROUTER_API_KEY  →  openrouter/anthropic/claude-3-5-haiku
  PROVIDER=gemini      →  GEMINI_API_KEY      →  gemini/gemini-1.5-flash
  PROVIDER=openai      →  OPENAI_API_KEY      →  openai/gpt-4o-mini
  MODEL=<slug>         →  override default model for the chosen provider
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

import dspy
import yaml
from dotenv import load_dotenv
from openevals.llm import create_llm_as_judge
from rich.console import Console

load_dotenv()

ROOT = Path(__file__).parent.parent
GITHUB_DIR = ROOT / ".github"
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

console = Console()

# ── Provider table ────────────────────────────────────────────────────────────
# model strings are litellm slugs — dspy.LM passes them straight to litellm
PROVIDERS: dict[str, dict[str, str | None]] = {
    "openrouter": {
        "model":       "openrouter/anthropic/claude-3-5-haiku",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_base":    "https://openrouter.ai/api/v1",
        "project":     None,
    },
    "gemini": {
        "model":       "gemini/models/gemini-2.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "api_base":    "https://generativelanguage.googleapis.com/v1beta/openai/",
        "project":     None,
    },
    "openai": {
        "model":       "openai/gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "api_base":    None,
        "project":     None,
    },
    "elitea": {
        "model":       "elitea/eu.anthropic.claude-sonnet-4-6",
        "api_key_env": "ELITEA_API_KEY",
        "api_base":    "https://next.elitea.ai/llm/v1",
        "project":     "1",
    },
}


def _provider_cfg() -> dict[str, Any]:
    name = os.environ.get("PROVIDER", "openrouter").lower()
    if name not in PROVIDERS:
        console.print(f"[red]Unknown PROVIDER '{name}'. Choose: {list(PROVIDERS)}[/red]")
        sys.exit(1)
    cfg: dict[str, Any] = dict(PROVIDERS[name])
    cfg["name"] = name
    cfg["model"] = os.environ.get("MODEL", cfg["model"])
    cfg["api_key"] = os.environ.get(cfg["api_key_env"], "")
    if not cfg["api_key"]:
        console.print(f"[red]{cfg['api_key_env']} not set.[/red]")
        sys.exit(1)
    return cfg


# ── DSPy LM setup ─────────────────────────────────────────────────────────────

def configure_dspy(cfg: dict) -> dspy.LM:
    kwargs: dict[str, Any] = {"api_key": cfg["api_key"]}
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    if cfg.get("project"):
        kwargs["extra_headers"] = {"X-Project": cfg["project"]}
    lm = dspy.LM(cfg["model"], **kwargs)
    dspy.configure(lm=lm)
    return lm


# ── openevals judge ───────────────────────────────────────────────────────────
# create_llm_as_judge returns a SimpleEvaluator callable.
# Calling it: judge(inputs=..., outputs=...) → EvaluatorResult TypedDict
#   EvaluatorResult = {"key": str, "score": float|bool, "comment": str|None, ...}
# With continuous=True the score is a float 0-1.
# With use_reasoning=False the inner scorer returns a raw float; the outer wrapper
# still packages it into EvaluatorResult, so result["score"] is always the right key.

def build_rubric(checks: list[dict]) -> str:
    criteria = "\n".join(
        f"- [{c['type']}] {c['value']}" for c in checks if c.get("value")
    ) or "- Does the response correctly follow the system prompt instructions?"
    return (
        "You are a strict evaluator. Score the following LLM output 0.0-1.0.\n"
        "Award 1.0 only if ALL criteria below are satisfied, 0.0 if any fail:\n"
        f"{criteria}\n\n"
        "System prompt + user turn (inputs): {inputs}\n"
        "Output to evaluate: {outputs}\n\n"
        "Return a single float between 0.0 and 1.0."
    )


def make_judge(cfg: dict, rubric: str):
    provider = cfg["name"]

    if provider in ("openrouter", "openai", "elitea"):
        from openai import OpenAI
        base_url = cfg.get("api_base") or "https://api.openai.com/v1"
        client_kwargs: dict[str, Any] = {"api_key": cfg["api_key"], "base_url": base_url}
        if cfg.get("project"):
            client_kwargs["default_headers"] = {"X-Project": cfg["project"]}
        client = OpenAI(**client_kwargs)
        bare_model = cfg["model"].split("/", 1)[-1] if "/" in cfg["model"] else cfg["model"]
        return create_llm_as_judge(
            prompt=rubric,
            judge=client,
            model=bare_model,
            continuous=True,
            use_reasoning=False,
        )

    # Gemini — langchain init_chat_model path
    return create_llm_as_judge(
        prompt=rubric,
        model=cfg["model"],
        continuous=True,
        use_reasoning=False,
    )


# ── Scenario + system prompt ──────────────────────────────────────────────────

def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.DOTALL).strip()


def resolve_system(raw: str) -> tuple[str, Path | None]:
    """Return (instructions_text, source_path_or_None)."""
    for candidate in (GITHUB_DIR / raw, Path(raw)):
        if candidate.exists() and candidate.is_file():
            return _strip_frontmatter(candidate.read_text(encoding="utf-8")), candidate
    return raw, None


def load_scenario(scenario_id: str) -> dict:
    for path in sorted((ROOT / "scenarios").glob("**/*.yaml")):
        with open(path, encoding="utf-8") as f:
            items = yaml.safe_load(f)
        if not isinstance(items, list):
            items = [items]
        for item in items:
            if item.get("id") == scenario_id:
                return item
    console.print(f"[red]Scenario '{scenario_id}' not found.[/red]")
    sys.exit(1)


# ── DSPy signature + program ──────────────────────────────────────────────────
# dspy.make_signature("input_field -> output_field", instructions=...) is the
# correct v3 API. The instructions become the rewritable part during MIPROv2.

def make_program(instructions: str) -> dspy.Predict:
    sig = dspy.make_signature("user_turn -> response", instructions=instructions)
    return dspy.Predict(sig)


# ── devset ────────────────────────────────────────────────────────────────────

def build_devset(scenario: dict) -> list[dspy.Example]:
    """
    Build examples from scenario.examples list (if present) or fall back to
    the single scenario user turn. Each Example must declare its input fields.
    """
    raw = scenario.get("examples") or [{"user_turn": scenario.get("user", "")}]
    return [
        dspy.Example(user_turn=ex.get("user_turn", ex.get("user", ""))).with_inputs("user_turn")
        for ex in raw
    ]


# ── metric ────────────────────────────────────────────────────────────────────
# DSPy metric signature: metric(example, pred, trace=None) → float | bool
# pred.response is the output field name from our signature.
# judge(inputs=..., outputs=...) → EvaluatorResult {"key":..., "score": float, ...}

def build_metric(judge: Callable, system_text: str) -> Callable:
    def metric(example: dspy.Example, pred: dspy.Prediction, trace=None) -> float:
        output: str = getattr(pred, "response", "") or ""
        if not output.strip():
            return 0.0
        result = judge(
            inputs={"system": system_text, "user": example.user_turn},
            outputs=output,
        )
        # EvaluatorResult is a TypedDict — access with ["score"], not .get()
        return float(result["score"])
    return metric


# ── evaluate ──────────────────────────────────────────────────────────────────

def run_evaluate(program: dspy.Module, devset: list, metric: Callable) -> float:
    # num_threads=1 avoids rate-limit bursts on small devsets
    evaluator = dspy.Evaluate(
        devset=devset,
        metric=metric,
        num_threads=1,
        display_progress=True,
        display_table=False,
    )
    return float(evaluator(program))


# ── optimize ──────────────────────────────────────────────────────────────────

def run_optimize(
    program: dspy.Module,
    devset: list,
    metric: Callable,
    optimizer_name: str,
) -> dspy.Module:
    if optimizer_name == "bootstrap":
        # BootstrapFewShotWithRandomSearch: finds good few-shot examples
        # compile(student, trainset=...) — no requires_permission_to_run
        opt = dspy.BootstrapFewShotWithRandomSearch(
            metric=metric,
            max_bootstrapped_demos=3,
            max_labeled_demos=3,
            num_candidate_programs=5,
        )
        return opt.compile(program, trainset=devset)

    # MIPROv2: rewrites instruction text + selects few-shot examples
    # requires_permission_to_run was removed in 3.x — do NOT pass it
    opt = dspy.MIPROv2(metric=metric, auto="light", verbose=False)
    return opt.compile(program, trainset=devset)


# ── save ──────────────────────────────────────────────────────────────────────

def save_results(
    optimized: dspy.Module,
    source_path: Path | None,
    scenario_id: str,
) -> str | None:
    """
    Save the DSPy program state to results/ and the rewritten instructions
    to a .optimized.md alongside the original .github/ file.
    Returns the rewritten instruction text (or None if unavailable).
    """
    state_path = RESULTS_DIR / f"{scenario_id}.optimized.json"
    optimized.save(str(state_path))
    console.print(f"Program state → [dim]{state_path}[/dim]")

    instructions = None
    for _, predictor in optimized.named_predictors():
        instructions = predictor.signature.instructions
        break

    if source_path and instructions:
        opt_path = source_path.with_suffix(".optimized.md")
        opt_path.write_text(instructions, encoding="utf-8")
        console.print(f"Rewritten instructions → [dim]{opt_path}[/dim]")

    return instructions


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DSPy + openevals prompt optimizer")
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--optimize", action="store_true",
                        help="Run optimizer after baseline eval")
    parser.add_argument("--optimizer", choices=["mipro", "bootstrap"], default="mipro",
                        help="mipro=MIPROv2 (rewrites instructions)  "
                             "bootstrap=BootstrapFewShotWithRandomSearch (adds examples)")
    args = parser.parse_args()

    cfg = _provider_cfg()
    console.print(
        f"\n[bold]Provider:[/bold] {cfg['name']}  "
        f"[bold]Model:[/bold] {cfg['model']}\n"
    )

    configure_dspy(cfg)

    scenario = load_scenario(args.scenario_id)
    system_text, source_path = resolve_system(scenario.get("system", ""))
    devset = build_devset(scenario)
    rubric = build_rubric(scenario.get("checks", []))
    judge = make_judge(cfg, rubric)
    metric = build_metric(judge, system_text)
    program = make_program(system_text)

    console.print(
        f"[bold]Baseline eval[/bold]  scenario=[cyan]{args.scenario_id}[/cyan]  "
        f"examples={len(devset)}"
    )
    baseline = run_evaluate(program, devset, metric)
    console.print(f"Baseline score: [yellow]{baseline:.3f}[/yellow]\n")

    if not args.optimize:
        return

    console.print(f"[bold]Optimizing[/bold] with [cyan]{args.optimizer}[/cyan]…\n")
    optimized = run_optimize(program, devset, metric, args.optimizer)

    console.print("\n[bold]Post-optimization eval[/bold]")
    final = run_evaluate(optimized, devset, metric)
    console.print(
        f"Optimized score: [green]{final:.3f}[/green]  "
        f"(Δ {final - baseline:+.3f})\n"
    )

    instructions = save_results(optimized, source_path, args.scenario_id)
    if instructions:
        console.print("\n[bold]Rewritten instructions (preview):[/bold]")
        console.print(instructions[:600] + ("…" if len(instructions) > 600 else ""))


if __name__ == "__main__":
    main()
