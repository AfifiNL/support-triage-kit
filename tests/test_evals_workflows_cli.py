from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ScriptedModel, out
from triage_kit import evals, signing
from triage_kit.api import create_app
from triage_kit.cli import main
from triage_kit.models import Category, Priority
from triage_kit.providers.mock import MockTriageModel
from triage_kit.store import Store

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "tickets.jsonl"
GATE = json.loads((ROOT / "evals" / "gate.json").read_text())


def test_dataset_is_consistent() -> None:
    items = evals.load_dataset(DATASET)
    assert len(items) == 60
    assert {i.split for i in items} == {"dev", "test"}
    for i in items:
        text = i.ticket.subject + "\n" + i.ticket.body
        for value in i.gold.pii + i.gold.names:
            assert value in text, (i.id, value)


def test_mock_passes_the_gate() -> None:
    scored = evals.run(evals.load_dataset(DATASET), MockTriageModel())
    assert evals.check_gate(evals.by_scope(scored), GATE, None) == []


def test_gate_catches_a_bad_model() -> None:
    lazy = ScriptedModel(out(Category.OTHER, Priority.LOW))
    scores = evals.by_scope(evals.run(evals.load_dataset(DATASET), lazy))
    failures = evals.check_gate(scores, GATE, None)
    assert any("category_accuracy" in f for f in failures)
    # The deterministic layers still hold with a useless model.
    assert scores["all"]["pii_recall"] == 1.0
    assert scores["all"]["escalation_recall"] == 1.0


def test_gate_catches_a_regression_against_the_baseline() -> None:
    scores = {"all": {"category_accuracy": 0.80, "model_errors": 0}}
    baseline = {"all": {"category_accuracy": 0.85}}
    gate = {"floors": {}, "max_drop": 0.02}
    assert evals.check_gate(scores, gate, baseline) == [
        "all: category_accuracy dropped 0.850 -> 0.800"
    ]


def test_model_errors_fail_the_gate() -> None:
    scores = {"all": {"model_errors": 1}}
    assert evals.check_gate(scores, {"floors": {}}, None) == ["all: 1 model errors"]


@pytest.mark.parametrize("name", ["ticket-intake.json", "deliver-approved-replies.json"])
def test_n8n_workflow_is_well_formed(name: str) -> None:
    wf = json.loads((ROOT / "workflows" / "n8n" / name).read_text())
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert len(nodes) == len(wf["nodes"]), "node names must be unique"
    known = {
        "n8n-nodes-base.webhook",
        "n8n-nodes-base.set",
        "n8n-nodes-base.crypto",
        "n8n-nodes-base.httpRequest",
        "n8n-nodes-base.respondToWebhook",
        "n8n-nodes-base.scheduleTrigger",
        "n8n-nodes-base.noOp",
    }
    triggers = {"n8n-nodes-base.webhook", "n8n-nodes-base.scheduleTrigger"}
    targets = set()
    for src, conn in wf["connections"].items():
        assert src in nodes
        for branch in conn["main"]:
            for edge in branch:
                assert edge["node"] in nodes
                targets.add(edge["node"])
    for n in wf["nodes"]:
        assert n["type"] in known
        assert "credentials" not in n, "no stored credentials in exported workflows"
        if n["type"] not in triggers:
            assert n["name"] in targets, f"{n['name']} is not connected"
    text = json.dumps(wf)
    assert "Bearer {{ $env.TRIAGE_REVIEW_TOKEN }}" in text or "TRIAGE_WEBHOOK_SECRET" in text


def test_workflows_call_routes_that_exist() -> None:
    app = create_app(Store(), MockTriageModel(), webhook_secret="x", review_token="y")
    paths = {getattr(r, "path", "") for r in app.routes}
    used = set()
    for f in (ROOT / "workflows" / "n8n").glob("*.json"):
        for n in json.loads(f.read_text())["nodes"]:
            url = n["parameters"].get("url", "")
            if url:
                path = url.split("}}", 1)[1].replace("{{ $json.id }}", "{oid}")
                used.add(path)
    assert used == {"/v1/webhooks/tickets", "/v1/outbox", "/v1/outbox/{oid}/ack"}
    assert used <= paths
    intake = (ROOT / "workflows" / "n8n" / "ticket-intake.json").read_text()
    assert signing.HEADER in intake


def test_cli_triage_and_demo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"id": "x", "subject": "Cancel", "body": "Please cancel my plan"}))
    assert main(["triage", str(f)]) == 0
    assert '"category": "cancellation"' in capsys.readouterr().out
    assert main(["demo"]) == 0
    shown = capsys.readouterr().out
    assert "unsigned request: 401" in shown
    model_view, outbox = shown.split("== 4.")
    assert "sam.rivera@example.org" not in model_view.split("== 3.")[0].split("== 2.")[1]
    assert "sam.rivera@example.org" in outbox  # restored only in the approved reply
