"""Eval set, metrics and the gate that fails CI when a score drops.

Metrics (per scope: all, dev, test):
  pii_recall          share of labelled e-mails, phone numbers, card numbers, IBANs and IP
                      addresses that no longer appear in the text the model sees (floor 1.0:
                      one leak fails the build)
  name_recall         the same for people's names (narrow patterns only; see the README)
  escalation_recall   share of labelled escalations (data-protection request, legal threat,
                      chargeback, safety) that the rules raised (floor 1.0)
  route_accuracy      queue matches the label
  category_accuracy, priority_accuracy
  urgent_recall       labelled-urgent tickets predicted urgent (a missed urgent ticket is the
                      costly error)
  draft_pass_rate     share of drafts that passed the draft checks
  model_errors        failed or missing model calls (replay misses count here)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from triage_kit.models import Category, Priority, Ticket, TriageResult
from triage_kit.pipeline import triage
from triage_kit.providers.base import TriageModel

HIGHER_IS_BETTER = (
    "pii_recall",
    "name_recall",
    "escalation_recall",
    "route_accuracy",
    "category_accuracy",
    "priority_accuracy",
    "urgent_recall",
    "draft_pass_rate",
)


class Gold(BaseModel):
    category: Category
    priority: Priority
    route: str
    escalations: list[str] = []
    pii: list[str] = []
    names: list[str] = []


class Item(BaseModel):
    id: str
    split: str
    ticket: Ticket
    gold: Gold


def load_dataset(path: Path) -> list[Item]:
    items = [
        Item.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item ids in the eval set")
    return items


def _leaked(value: str, text: str) -> bool:
    """A value counts as leaked if any meaningful part of it is still visible, not only the
    whole string: the local part of an e-mail address, or any run of 4+ letters or digits
    (one group of a phone number, an IBAN or a card number)."""
    low = text.lower()
    if "@" in value:
        local = value.split("@", 1)[0]
        return (local if len(local) >= 3 else value).lower() in low
    parts = [p for p in re.findall(r"[^\W_]+", value) if len(p) >= 4] or [value]
    return any(p.lower() in low for p in parts)


def _name_leaked(name: str, text: str) -> bool:
    tokens = [t for t in re.findall(r"[^\W\d_][\w'\u2019-]*", name) if len(t) >= 2]
    return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text) for t in tokens)


@dataclass
class Scored:
    item: Item
    result: TriageResult

    def leaks(self) -> list[str]:
        return [v for v in self.item.gold.pii if _leaked(v, self.result.redacted_text)]

    def name_leaks(self) -> list[str]:
        return [v for v in self.item.gold.names if _name_leaked(v, self.result.redacted_text)]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def metrics(rows: Iterable[Scored]) -> dict[str, Any]:
    rows = list(rows)
    n = len(rows)
    pii_total = sum(len(r.item.gold.pii) for r in rows)
    pii_leaked = sum(len(r.leaks()) for r in rows)
    names_total = sum(len(r.item.gold.names) for r in rows)
    names_leaked = sum(len(r.name_leaks()) for r in rows)
    esc_total = sum(len(r.item.gold.escalations) for r in rows)
    esc_hit = sum(len(set(r.item.gold.escalations) & set(r.result.escalations)) for r in rows)
    esc_extra = sum(len(set(r.result.escalations) - set(r.item.gold.escalations)) for r in rows)
    urgent = [r for r in rows if r.item.gold.priority is Priority.URGENT]
    drafted = [r for r in rows if r.result.status != "model_error"]
    return {
        "items": n,
        "pii_recall": _ratio(pii_total - pii_leaked, pii_total),
        "pii_values": pii_total,
        "name_recall": _ratio(names_total - names_leaked, names_total),
        "escalation_recall": _ratio(esc_hit, esc_total),
        "false_escalations": esc_extra,
        "route_accuracy": _ratio(sum(r.result.route == r.item.gold.route for r in rows), n),
        "category_accuracy": _ratio(
            sum(r.result.category is r.item.gold.category for r in rows), n
        ),
        "priority_accuracy": _ratio(
            sum(r.result.priority is r.item.gold.priority for r in rows), n
        ),
        "urgent_recall": _ratio(
            sum(r.result.priority is Priority.URGENT for r in urgent), len(urgent)
        ),
        "draft_pass_rate": _ratio(
            sum(r.result.status == "ready_for_review" for r in drafted), len(drafted)
        ),
        "model_errors": n - len(drafted),
    }


def run(items: list[Item], model: TriageModel, workers: int = 1) -> list[Scored]:
    def one(item: Item) -> Scored:
        return Scored(item, triage(item.ticket, model))

    if workers <= 1:
        return [one(i) for i in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, items))


def by_scope(scored: list[Scored]) -> dict[str, dict[str, Any]]:
    out = {"all": metrics(scored)}
    for split in sorted({s.item.split for s in scored}):
        out[split] = metrics(s for s in scored if s.item.split == split)
    return out


def check_gate(
    scores: dict[str, dict[str, Any]], gate: dict[str, Any], baseline: dict[str, Any] | None
) -> list[str]:
    failures: list[str] = []
    floors: dict[str, float] = gate["floors"]
    max_drop: float = gate.get("max_drop", 0.02)
    for scope, m in scores.items():
        if m["model_errors"] > gate.get("max_model_errors", 0):
            failures.append(f"{scope}: {m['model_errors']} model errors")
        for name, floor in floors.items():
            value = m.get(name)
            if value is not None and value < floor:
                failures.append(f"{scope}: {name} {value:.3f} < floor {floor:.3f}")
        if baseline and scope in baseline:
            for name in HIGHER_IS_BETTER:
                old, new = baseline[scope].get(name), m.get(name)
                if old is not None and new is not None and new < old - max_drop:
                    failures.append(f"{scope}: {name} dropped {old:.3f} -> {new:.3f}")
    return failures


def report_markdown(scores: dict[str, dict[str, Any]], scored: list[Scored], model: str) -> str:
    cols = ["items", *HIGHER_IS_BETTER, "false_escalations", "model_errors"]
    lines = [
        f"# Eval report ({model})",
        "",
        "| scope | " + " | ".join(cols) + " |",
        "|---" * (len(cols) + 1) + "|",
    ]
    for scope, m in scores.items():
        cells = ["-" if m.get(c) is None else str(m.get(c)) for c in cols]
        lines.append(f"| {scope} | " + " | ".join(cells) + " |")
    lines += ["", "## Misses", ""]
    for s in scored:
        g, r = s.item.gold, s.result
        diffs = []
        if r.category is not g.category:
            diffs.append(f"category {r.category.value} (gold {g.category.value})")
        if r.priority is not g.priority:
            diffs.append(f"priority {r.priority.value} (gold {g.priority.value})")
        if r.route != g.route:
            diffs.append(f"route {r.route} (gold {g.route})")
        missing = sorted(set(g.escalations) - set(r.escalations))
        if missing:
            diffs.append(f"missed escalations {missing}")
        if s.leaks():
            diffs.append(f"{len(s.leaks())} personal-data value(s) leaked")
        if s.name_leaks():
            diffs.append(f"{len(s.name_leaks())} name(s) not redacted")
        if r.draft_findings:
            diffs.append(f"draft: {'; '.join(r.draft_findings)}")
        if diffs:
            lines.append(f"- `{s.item.id}` ({s.item.split}): " + "; ".join(diffs))
    return "\n".join(lines) + "\n"


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
