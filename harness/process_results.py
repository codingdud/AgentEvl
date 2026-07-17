"""
harness/process_results.py
 
Process test results JSON and generate optimization reports for failed scenarios.
 
Run:
  uv run python -m harness.process_results
  uv run python -m harness.process_results --results results/run-20260716T094212Z.json
  uv run python -m harness.process_results --results results/run-*.json --optimize
  uv run python -m harness.process_results --results results/run-*.json --optimize --batch
"""
 
from __future__ import annotations
 
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
 
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
 
console = Console()
 
ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "results"
 
 
def load_results(results_file: Path) -> dict[str, Any]:
    """Load results JSON file."""
    if not results_file.exists():
        console.print(f"[red]Results file not found: {results_file}[/red]")
        sys.exit(1)
   
    with open(results_file) as f:
        return json.load(f)
 
 
def analyze_results(results: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Separate passed and failed scenarios."""
    passed = [r for r in results["results"] if r["passed"]]
    failed = [r for r in results["results"] if not r["passed"]]
    return passed, failed
 
 
def generate_optimization_report(
    results_file: Path,
    passed: list[dict],
    failed: list[dict],
    meta: dict,
) -> None:
    """Generate a markdown optimization report."""
    report_path = results_file.with_suffix(".report.md")
   
    # Build the report
    lines = [
        "# Test Results & Optimization Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Provider:** {meta.get('provider', 'unknown')}",
        f"**Model:** {meta.get('model', 'unknown')}",
        f"**Total Tokens:** {meta.get('tokens', {}).get('total', 0):,}",
        f"**Total Cost:** ${meta.get('cost_usd', 0):.4f}",
        "",
        "---",
        "",
        f"## Summary",
        f"- **Passed:** {len(passed)}/{len(passed) + len(failed)}",
        f"- **Failed:** {len(failed)}/{len(passed) + len(failed)}",
        "",
    ]
   
    # Passed scenarios
    if passed:
        lines.extend([
            "## ✅ Passed Scenarios",
            "",
        ])
        for r in passed:
            lines.append(f"- **{r['id']}** ({r['category']} / {r['target']})")
            if r.get("description"):
                lines.append(f"  - {r['description']}")
        lines.append("")
   
    # Failed scenarios
    if failed:
        lines.extend([
            "## ❌ Failed Scenarios (Optimization Candidates)",
            "",
        ])
        for r in failed:
            lines.append(f"### {r['id']}")
            if r.get("description"):
                lines.append(f"**Description:** {r['description']}")
            lines.append(f"**Category:** {r['category']} / {r['target']}")
           
            # Check results
            if r.get("checks"):
                lines.append("**Failed Checks:**")
                for check in r["checks"]:
                    if not check.get("passed"):
                        lines.append(f"  - [{check['type']}] {check['label']}")
            elif r.get("error"):
                lines.append(f"**Error:** {r['error']}")
           
            lines.append(f"**Tokens:** {r.get('tok_in', 0):,} in / {r.get('tok_out', 0):,} out")
            lines.append("")
            lines.append(f"`uv run python -m harness.optimizer --scenario-id {r['id']} --optimize`")
            lines.append("")
       
        # Quick optimization script
        failed_ids = [r['id'] for r in failed]
        lines.extend([
            "## 🚀 Batch Optimization Command",
            "",
            "Run all failed scenarios through optimizer:",
            "",
            "```bash",
        ])
        for scenario_id in failed_ids:
            lines.append(f"uv run python -m harness.optimizer --scenario-id {scenario_id} --optimize &")
        lines.extend([
            "wait",
            "```",
            "",
        ])
   
    report_content = "\n".join(lines)
    report_path.write_text(report_content, encoding="utf-8")
    console.print(f"Report saved: [dim]{report_path}[/dim]")
    return report_path
 
 
def display_results_table(passed: list[dict], failed: list[dict]) -> None:
    """Display results in a rich table."""
    table = Table(title="Test Results Summary", show_lines=True)
    table.add_column("ID", style="cyan")
    table.add_column("Category")
    table.add_column("Status", justify="center")
    table.add_column("Issue")
   
    for r in passed:
        table.add_row(
            r["id"],
            r.get("category", ""),
            "[green]✓ PASS[/green]",
            "—",
        )
   
    for r in failed:
        issue = ""
        if r.get("checks"):
            failed_checks = [c.get("label_short", c["label"]) for c in r["checks"] if not c.get("passed")]
            issue = ", ".join(failed_checks[:2])  # Show first 2
            if len(failed_checks) > 2:
                issue += f" (+{len(failed_checks) - 2} more)"
        elif r.get("error"):
            issue = r["error"][:60] + ("…" if len(r["error"]) > 60 else "")
       
        table.add_row(
            r["id"],
            r.get("category", ""),
            "[red]✗ FAIL[/red]",
            issue,
        )
   
    console.print(table)
 
 
def run_optimizer_batch(failed: list[dict], scenario_dir: Path | None = None) -> None:
    """Run optimizer on all failed scenarios."""
    if not failed:
        console.print("[yellow]No failed scenarios to optimize.[/yellow]")
        return
   
    failed_ids = [r["id"] for r in failed]
    console.print(f"\n[bold]Running optimizer on {len(failed_ids)} failed scenario(s)...[/bold]\n")
   
    cmd_base = ["uv", "run", "python", "-m", "harness.optimizer", "--optimize"]
    if scenario_dir:
        cmd_base.extend(["--scenario-dir", str(scenario_dir)])
   
    for scenario_id in failed_ids:
        cmd = cmd_base + ["--scenario-id", scenario_id]
        console.print(f"[cyan]► {scenario_id}[/cyan]")
        try:
            subprocess.run(cmd, check=True, cwd=ROOT)
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to optimize {scenario_id}: {e}[/red]")
 
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process test results and generate optimization reports"
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Optional results path/pattern (defaults to latest results/run-*.json)",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Automatically run optimizer on failed scenarios",
    )
    parser.add_argument(
        "--scenario-dir",
        type=Path,
        help="Path to scenarios directory (optional, for --optimize)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run all failed optimizations in parallel (requires --optimize)",
    )
    args = parser.parse_args()
 
    # Expand explicit glob, direct file path, or default to latest run file.
    if args.results is None:
        candidates = sorted(RESULTS_DIR.glob("run-*.json"))
        if not candidates:
            console.print(f"[red]No run files found in: {RESULTS_DIR}[/red]")
            sys.exit(1)
        results_files = [candidates[-1]]
        console.print(f"[dim]Auto-selected latest results file: {results_files[0].name}[/dim]")
    elif any(ch in str(args.results) for ch in "*?[]"):
        results_files = sorted(args.results.parent.glob(args.results.name))
    elif args.results.exists():
        results_files = [args.results]
    else:
        results_files = []
 
    if not results_files:
        console.print(f"[red]No results files matching: {args.results}[/red]")
        sys.exit(1)
 
    # Process each results file
    for results_file in results_files:
        console.print(f"\n[bold]Processing: {results_file.name}[/bold]\n")
       
        results = load_results(results_file)
        meta = results.get("meta", {})
        passed, failed = analyze_results(results)
       
        # Display summary table
        display_results_table(passed, failed)
       
        # Generate report
        console.print()
        report_path = generate_optimization_report(results_file, passed, failed, meta)
       
        # Show optimization summary
        if failed:
            panel = Panel(
                f"[yellow]{len(failed)} scenario(s) failed and need optimization.[/yellow]\n"
                f"See [cyan]{report_path.name}[/cyan] for details.",
                title="⚠️  Optimization Needed",
                expand=False,
            )
            console.print(panel)
           
            # Run optimizer if requested
            if args.optimize:
                if args.batch:
                    console.print("\n[bold]Note:[/bold] Sequential mode (parallel batch requires job control)")
                run_optimizer_batch(failed, args.scenario_dir)
        else:
            panel = Panel(
                "[green]All scenarios passed! No optimization needed.[/green]",
                title="✅ Success",
                expand=False,
            )
            console.print(panel)
 
 
if __name__ == "__main__":
    main()