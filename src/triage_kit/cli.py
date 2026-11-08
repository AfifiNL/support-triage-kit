"""support-triage-kit command line: triage, eval, demo, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from triage_kit import evals
from triage_kit.factory import build_model
from triage_kit.models import Ticket
from triage_kit.pipeline import triage
from triage_kit.providers.replay import RecordingTriageModel, ReplayTriageModel


def _cmd_triage(args: argparse.Namespace) -> int:
    ticket = Ticket.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
    result = triage(ticket, build_model())
    print(result.model_dump_json(indent=2))
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    items = evals.load_dataset(Path(args.dataset))
    if args.split != "all":
        items = [i for i in items if i.split == args.split]
    model = build_model()
    scored = evals.run(items, model, workers=args.workers)
    scores = evals.by_scope(scored)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(
        evals.report_markdown(scores, scored, model.name), encoding="utf-8"
    )
    evals.save_json(out / "scores.json", scores)
    for scope, m in scores.items():
        shown = {k: m[k] for k in ("items", *evals.HIGHER_IS_BETTER, "model_errors")}
        print(f"{scope:>5}: " + "  ".join(f"{k}={v}" for k, v in shown.items()))
    if isinstance(model, RecordingTriageModel):
        model.cassette.save()
        print(
            f"recorded {len(model.cassette.entries)} calls ({model.reused} reused) "
            f"to {model.cassette.path}; {len(model.cassette.errors)} failed"
        )
        if model.cassette.errors:
            return 1
    if isinstance(model, ReplayTriageModel) and model.cassette.misses:
        print(
            f"REPLAY MISS for {len(model.cassette.misses)} ticket(s): the model input changed;"
            " re-record with `make record-live`"
        )
        return 1
    if args.update_baseline:
        evals.save_json(Path(args.baseline), scores)
        print(f"baseline written to {args.baseline}; review the diff before committing")
        return 0
    if args.gate:
        gate = json.loads(Path(args.gate).read_text(encoding="utf-8"))
        base_path = Path(args.baseline)
        baseline = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else None
        failures = evals.check_gate(scores, gate, baseline)
        if failures:
            print("GATE FAILED:\n  " + "\n  ".join(failures))
            return 1
        print("gate passed")
    return 0


def _cmd_demo(_: argparse.Namespace) -> int:
    from triage_kit.demo import run_demo

    return run_demo()


def _cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - runs a server
    import uvicorn

    from triage_kit.api import app_from_env

    uvicorn.run(app_from_env(), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="support-triage-kit", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triage", help="triage one ticket (JSON file) and print the result")
    t.add_argument("file")
    t.set_defaults(fn=_cmd_triage)
    e = sub.add_parser("eval", help="run the eval set; with --gate, fail on a drop")
    e.add_argument("--dataset", default="evals/tickets.jsonl")
    e.add_argument("--split", default="all", choices=["all", "dev", "test"])
    e.add_argument("--gate", default=None)
    e.add_argument("--baseline", default="evals/baseline.json")
    e.add_argument("--update-baseline", action="store_true")
    e.add_argument("--workers", type=int, default=1)
    e.add_argument("--out", default="reports/eval")
    e.set_defaults(fn=_cmd_eval)
    d = sub.add_parser("demo", help="offline end-to-end demo: webhook, review, outbox")
    d.set_defaults(fn=_cmd_demo)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default=os.environ.get("TRIAGE_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("TRIAGE_PORT", "8000")))
    s.set_defaults(fn=_cmd_serve)
    args = p.parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
