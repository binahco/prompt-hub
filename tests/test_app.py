from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from llm_client import TokenUsage
from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

from prompt_hub.main import DEFAULT_DATASET, build_client, judge_for, scan_fleet

STUB_TEXT = json.dumps(
    {
        "repo": "triage-bot",
        "prompt_id": "issue-triage-generator",
        "purpose": "clasificar los issues abiertos de la flota",
        "when_to_use": "en el ciclo matutino, antes de trabajar",
        "inputs": ["date", "repos", "issues"],
        "output": "JSON issue-triage-v1",
    },
    ensure_ascii=False,
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(
            text=self.text, model=request.model, usage=TokenUsage(input_tokens=10, output_tokens=5)
        )

    def stream(self, request: ProviderRequest):
        yield self.text


def mini_repo(tmp_path: Path, name: str, prompt_id: str) -> Path:
    repo = tmp_path / name
    (repo / "prompts").mkdir(parents=True, exist_ok=True)
    (repo / "core-consumer.yml").write_text(
        f"week: 1\nname: {name}\ncore_version: 1.1.1\nreused_modules: [llm-client]\n"
        "estimated_new_surface: 20\n"
    )
    (repo / "prompts" / "demo.md").write_text(
        "---\n"
        f"id: {prompt_id}\n"
        "version: 0.1.0\n"
        "schema: demo-v1\n"
        "eval: evals/demo.jsonl\n"
        "---\n"
        "## Sistema\n\nResponde con la forma {\"date\": \"...\"} sin cambiar nada.\n\n"
        "## Usuario\n\nFecha: {date}\nHechos: {facts}\n"
    )
    return repo


def make_client(tmp_path, stub=None):
    from prompt_hub.main import create_app

    fleet = scan_fleet([mini_repo(tmp_path, "repo-a", "prompt-uno"), mini_repo(tmp_path, "repo-b", "prompt-dos")])
    app = create_app(provider=stub or StubProvider(), fleet=fleet)
    return TestClient(app)


def test_health_reusa_web_api_base(tmp_path) -> None:
    body = make_client(tmp_path).get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "prompt-hub"
    assert body["week"] == 17


def test_el_catalogo_lista_y_detalla_con_parser_io(tmp_path) -> None:
    client = make_client(tmp_path)
    listing = client.get("/prompts").json()
    assert listing["count"] == 2
    assert {p["id"] for p in listing["prompts"]} == {"prompt-uno", "prompt-dos"}

    detail = client.get("/prompts/repo-a/prompt-uno").json()
    titulos = [s["title"] for s in detail["sections"]]
    assert titulos == ["Sistema", "Usuario"]
    primera = detail["sections"][0]
    assert primera["line_start"] > 0 and primera["body"], "parser-io no aportó estructura"

    assert client.get("/prompts/repo-a/no-existe").status_code == 404


def test_render_formatea_solo_la_seccion_usuario_y_redacta(tmp_path) -> None:
    client = make_client(tmp_path)
    resp = client.post(
        "/prompts/repo-a/prompt-uno/render",
        json={"variables": {"date": "2026-11-20", "facts": "token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456 en el echo"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "Fecha: 2026-11-20" in body["rendered"]
    assert "ghp_" not in body["rendered"], "el secreto llegó al render"
    assert body["redactions"] == 1
    # el Sistema tiene llaves de JSON literales: renderizar no puede explotar por ellas
    assert '{"date"' in client.get("/prompts/repo-a/prompt-uno").json()["sections"][0]["body"]


def test_render_con_variable_faltante_es_422(tmp_path) -> None:
    resp = make_client(tmp_path).post("/prompts/repo-a/prompt-uno/render", json={"variables": {"date": "x"}})
    assert resp.status_code == 422
    assert resp.json()["error"] == "falta_variable"


def test_describe_pasa_por_el_validador(tmp_path) -> None:
    client = make_client(tmp_path)
    ok = client.post("/describe", json={"repo": "repo-a", "prompt_id": "prompt-uno"})
    assert ok.status_code == 200
    assert ok.json()["validation_ok"] is True
    assert ok.json()["parsed"]["purpose"] == "clasificar los issues abiertos de la flota"

    roto = make_client(tmp_path, stub=StubProvider(text="no es json"))
    resp = roto.post("/describe", json={"repo": "repo-a", "prompt_id": "prompt-uno"})
    assert resp.status_code == 422


def test_el_judge_evoluciona_el_dataset_completo(tmp_path) -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    client = build_client(StubProvider())
    out = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert out.total == 3
    assert out.passed == 3
