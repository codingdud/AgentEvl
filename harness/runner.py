"""
Agent / Skill / MCP / Prompt test harness.

All agent, skill, and prompt files live under .github/ — the harness reads them from there.

Provider is controlled by PROVIDER in .env (openrouter | gemini | openai).
See .env.example for all keys.

Run:
  uv run python -m harness.runner
  uv run python -m harness.runner --category prompts
  uv run python -m harness.runner --id my-scenario-id
  uv run python -m harness.runner --list
  uv run python -m harness.runner --no-save
  uv run python -m harness.runner --scenario-dir custom/path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import time

import yaml
from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.table import Table

load_dotenv()

ROOT = Path(__file__).parent.parent
GITHUB_DIR = ROOT / ".github"
SCENARIOS_DIR = ROOT / "scenarios"
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)

console = Console()

# ── Provider config (mirrors optimizer.py) ────────────────────────────────────

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
        "project":     "1",           # passed as X-Project header
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


def _client(cfg: dict) -> OpenAI:
    kwargs: dict[str, Any] = {"api_key": cfg["api_key"]}
    if cfg.get("api_base"):
        kwargs["base_url"] = cfg["api_base"]
    if cfg.get("project"):
        kwargs["default_headers"] = {"X-Project": cfg["project"]}
    return OpenAI(**kwargs)


# ── Token / cost tracking ─────────────────────────────────────────────────────
# Pricing per 1M tokens (input / output).  Override via COST_INPUT / COST_OUTPUT in .env.
# Default: $1.00 / $1.00 per 1M  (conservative placeholder — set real values in .env)
DEFAULT_COST_PER_1M = {"input": 1.00, "output": 1.00}

_session_tokens: dict[str, int] = {"input": 0, "output": 0, "total": 0}


def _record_usage(usage) -> tuple[int, int]:
    """Add usage from one API response to the session totals. Returns (in, out)."""
    if usage is None:
        return 0, 0
    inp = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["total"] += inp + out
    return inp, out


def _cost(tokens: int, rate_per_1m: float) -> float:
    return tokens / 1_000_000 * rate_per_1m


def call_llm(system: str, user: str, cfg: dict, model_override: str | None = None) -> str:
    model = model_override or cfg["model"]
    # Strip the leading provider prefix only:
    # "openrouter/anthropic/claude-3-5-haiku" -> "anthropic/claude-3-5-haiku"
    # "openai/gpt-4o-mini"                    -> "gpt-4o-mini"
    # "gemini/models/gemini-2.5-flash"        -> "models/gemini-2.5-flash"
    # "elitea/eu.anthropic.claude-sonnet-4-6" -> "eu.anthropic.claude-sonnet-4-6"
    slug = model.split("/", 1)[-1] if "/" in model else model
    client = _client(cfg)
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=slug,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=4096,
            )
            inp, out = _record_usage(resp.usage)
            console.print(
                f"    [dim]tokens: {inp} in / {out} out[/dim]",
                highlight=False,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            msg = str(exc)
            if "429" in msg and attempt < 3:
                wait = 15 * (attempt + 1)
                console.print(f"  [yellow]429 rate-limit, waiting {wait}s (attempt {attempt+1}/3)...[/yellow]")
                time.sleep(wait)
            else:
                raise
    return ""


# ── .github/ discovery ──────────────────────────────────────────────────────

def list_github_files() -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {"agents": [], "skills": [], "prompts": []}
    for key in result:
        d = GITHUB_DIR / key
        if d.exists():
            result[key] = sorted(d.rglob("*.md")) + sorted(d.rglob("*.yaml")) + sorted(d.rglob("*.yml"))
    return result


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.DOTALL).strip()


def resolve_system(raw: str) -> str:
    for candidate in (GITHUB_DIR / raw, Path(raw)):
        if candidate.exists() and candidate.is_file():
            return _strip_frontmatter(candidate.read_text(encoding="utf-8"))
    return raw


# ── Scenario loading ────────────────────────────────────────────────────────

def load_scenarios(
    scenario_dir: Path,
    category: str | None = None,
    scenario_id: str | None = None,
) -> list[dict]:
    """Load scenarios from scenario_dir (supports .yaml and .yml files)."""
    scenarios: list[dict] = []
    if category:
        glob_patterns = [f"{category}/*.yaml", f"{category}/*.yml"]
    else:
        glob_patterns = ["**/*.yaml", "**/*.yml"]
    
    for pattern in glob_patterns:
        for path in sorted(scenario_dir.glob(pattern)):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, list):
                scenarios.extend(data)
            else:
                scenarios.append(data)
    
    if scenario_id:
        scenarios = [s for s in scenarios if s.get("id") == scenario_id]
    
    return scenarios


# ── Checks ──────────────────────────────────────────────────────────────────

def _check_contains(output: str, value: str) -> tuple[bool, str]:
    return value.lower() in output.lower(), f"contains '{value}'"


def _check_not_contains(output: str, value: str) -> tuple[bool, str]:
    return value.lower() not in output.lower(), f"not_contains '{value}'"


def _check_llm_judge(output: str, value: str, cfg: dict) -> tuple[bool, str, str]:
    verdict = call_llm(
        "You are a strict pass/fail evaluator. Answer ONLY 'PASS' or 'FAIL'.",
        f"Criterion: {value}\n\nOutput to evaluate:\n{output}",
        cfg,
    )
    passed = "PASS" in verdict.upper()
    return passed, f"llm_judge: {value}", f"llm_judge: {value[:60]}..."


def run_checks(output: str, checks: list[dict], cfg: dict) -> list[dict]:
    results = []
    for chk in checks:
        ctype = chk["type"]
        val = chk["value"]
        if ctype == "contains":
            passed, label = _check_contains(output, val)
            label_short = label
        elif ctype == "not_contains":
            passed, label = _check_not_contains(output, val)
            label_short = label
        elif ctype == "llm_judge":
            passed, label, label_short = _check_llm_judge(output, val, cfg)
        else:
            passed, label, label_short = False, f"unknown type '{ctype}'", f"unknown type '{ctype}'"
        results.append({"type": ctype, "passed": passed, "label": label, "label_short": label_short})
    return results


# ── Runner ──────────────────────────────────────────────────────────────────

def run_scenario(scenario: dict, cfg: dict) -> dict[str, Any]:
    system = resolve_system(scenario.get("system", "You are a helpful assistant."))
    user = scenario.get("user", "")
    checks = scenario.get("checks", [])
    tok_before = (_session_tokens["input"], _session_tokens["output"])
    try:
        output = call_llm(system, user, cfg, model_override=scenario.get("model"))
        check_results = run_checks(output, checks, cfg)
        passed = all(c["passed"] for c in check_results)
        error = None
    except Exception as exc:
        output, check_results, passed, error = "", [], False, str(exc)

    return {
        "id": scenario.get("id", "unknown"),
        "category": scenario.get("category", ""),
        "target": scenario.get("target", ""),
        "description": scenario.get("description", ""),
        "passed": passed,
        "checks": check_results,
        "output_snippet": output[:300],
        "error": error,
        "tok_in":  _session_tokens["input"]  - tok_before[0],
        "tok_out": _session_tokens["output"] - tok_before[1],
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent/Skill/MCP/Prompt test harness")
    parser.add_argument("--category", help="agents | skills | mcps | prompts")
    parser.add_argument("--id", dest="scenario_id", help="Run a single scenario by id")
    parser.add_argument("--scenario-dir", type=Path, default=SCENARIOS_DIR,
                        help="Path to scenarios directory (default: promtEvl/scenarios)")
    parser.add_argument("--no-save", action="store_true", help="Skip writing results JSON")
    parser.add_argument("--list", action="store_true", help="List discovered .github/ files and exit")
    args = parser.parse_args()

    if args.list:
        files = list_github_files()
        for section, paths in files.items():
            console.print(f"\n[bold].github/{section}/[/bold]")
            if paths:
                for p in paths:
                    console.print(f"  {p.relative_to(GITHUB_DIR)}")
            else:
                console.print("  [dim](empty)[/dim]")
        return

    cfg = _provider_cfg()
    console.print(f"\n[bold]Provider:[/bold] {cfg['name']}  [bold]Model:[/bold] {cfg['model']}\n")

    scenarios = load_scenarios(
        scenario_dir=args.scenario_dir,
        category=args.category,
        scenario_id=args.scenario_id,
    )
    if not scenarios:
        console.print("[yellow]No scenarios found.[/yellow]")
        return

    console.print(f"[bold]Running {len(scenarios)} scenario(s)[/bold]\n")
    all_results = []
    for i, s in enumerate(scenarios):
        if i > 0:
            time.sleep(10)   # avoid back-to-back 429s on free-tier quota
        console.print(f"  >> [cyan]{s.get('id')}[/cyan]  ({s.get('category')} / {s.get('target')})")        
        all_results.append(run_scenario(s, cfg))

    rate_in  = float(os.environ.get("COST_INPUT",  DEFAULT_COST_PER_1M["input"]))
    rate_out = float(os.environ.get("COST_OUTPUT", DEFAULT_COST_PER_1M["output"]))

    table = Table(title="Test Results", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Cat")
    table.add_column("Target")
    table.add_column("Checks")
    table.add_column("In tok/cost",    justify="right")
    table.add_column("Out tok/cost",   justify="right")
    table.add_column("Total tok/cost", justify="right")
    table.add_column("Result",         justify="center")

    total_pass = 0
    for r in all_results:
        t_in, t_out  = r["tok_in"], r["tok_out"]
        c_in  = _cost(t_in,  rate_in)
        c_out = _cost(t_out, rate_out)
        checks_summary = ", ".join(
            f"{'OK' if c['passed'] else 'FAIL'} {c.get('label_short', c['label'])}" for c in r["checks"]
        ) or (r["error"] or "no checks")
        status = "[green]PASS[/green]" if r["passed"] else "[red]FAIL[/red]"
        total_pass += int(r["passed"])
        table.add_row(
            r["id"],
            r["category"],
            r["target"],
            checks_summary,
            f"{t_in:,} / ${c_in:.4f}",
            f"{t_out:,} / ${c_out:.4f}",
            f"{t_in + t_out:,} / ${c_in + c_out:.4f}",
            status,
        )

    agg_in  = _session_tokens["input"]
    agg_out = _session_tokens["output"]
    agg_ci  = _cost(agg_in,  rate_in)
    agg_co  = _cost(agg_out, rate_out)
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", "", "",
        f"[bold]{agg_in:,} / ${agg_ci:.4f}[/bold]",
        f"[bold]{agg_out:,} / ${agg_co:.4f}[/bold]",
        f"[bold]{agg_in + agg_out:,} / ${agg_ci + agg_co:.4f}[/bold]",
        f"[bold]{total_pass}/{len(all_results)}[/bold]",
    )
    console.print(table)
    console.print()

    cost_total = agg_ci + agg_co
    if not args.no_save:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = RESULTS_DIR / f"run-{ts}.json"
        payload = {
            "meta": {
                "provider": cfg["name"],
                "model": cfg["model"],
                "tokens": dict(_session_tokens),
                "cost_usd": round(cost_total, 6),
            },
            "results": all_results,
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"Results saved: [dim]{out}[/dim]\n")


if __name__ == "__main__":
    main()
