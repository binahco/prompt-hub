from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from fastapi import FastAPI
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from parser_io import parse
from pydantic import BaseModel
from schema_validate import SchemaRegistry
from secure_base import redact, sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run
from web_api_base import CompleteRequest, LLMHTTPError, create_app as make_web_app, llm_complete

from .models import PromptCard

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "prompt-card-v1"
PROMPT_ID = "prompt-card-generator"
ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / "prompts"
DEFAULT_PROMPT = PROMPT_DIR / "prompt-card-generator.md"
DEFAULT_DATASET = ROOT / "evals" / "prompt-card-generator.jsonl"
EXCERPT_CHARS = 600
DESCRIBE_TTL_SECONDS = 86400.0


# --------------------------------------------------------------------------
# prompt propio (la ficha LLM)
# --------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_card_prompt(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    """Prompt templado para `prompt-card-generator`; cualquier otro prompt pasa crudo."""
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_part = body.split("## Usuario\n", 1)[1].strip().format(**variables)
        return [
            {"role": "system", "content": system_part},
            {"role": "user", "content": user_part},
        ]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file: Path | None = None) -> LlmClient:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=DESCRIBE_TTL_SECONDS, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, PromptCard)
    return LlmClient(
        provider,
        consumer_repo="prompt-hub",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_card_prompt,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )


# --------------------------------------------------------------------------
# inventario determinista: la flota leída con parser-io (tercer uso, D12)
# --------------------------------------------------------------------------
def scan_repo(repo_dir: Path) -> list[dict]:
    cards = []
    prompts_dir = repo_dir / "prompts"
    if not prompts_dir.is_dir():
        return cards
    for path in sorted(prompts_dir.glob("*.md")):
        text = path.read_text()
        if not text.startswith("---"):
            continue
        _, frontmatter, _ = text.split("---", 2)
        meta = yaml.safe_load(frontmatter) or {}
        doc = parse(path)
        cards.append(
            {
                "repo": repo_dir.name,
                "path": f"prompts/{path.name}",
                "id": str(meta.get("id", path.stem)),
                "version": str(meta.get("version", "")),
                "schema": meta.get("schema"),
                "eval": meta.get("eval"),
                "sections": [
                    {
                        "title": s.title,
                        "line_start": s.line_start,
                        "line_end": s.line_end,
                        "body": s.body,
                    }
                    for s in doc.sections
                ],
                "lines": doc.lines,
                "approx_tokens": doc.approx_tokens,
            }
        )
    return cards


def discover_fleet(projects_dir: Path) -> list[Path]:
    """Hermanos con core-consumer.yml: la flota, sin barrer proyectos ajenos."""
    return sorted(p for p in projects_dir.iterdir() if (p / "core-consumer.yml").is_file())


def scan_fleet(repo_dirs: list[Path]) -> list[dict]:
    out: list[dict] = []
    for repo in repo_dirs:
        out.extend(scan_repo(repo))
    return sorted(out, key=lambda c: (c["repo"], c["id"]))


def _section(card: dict, title: str) -> dict | None:
    return next((s for s in card["sections"] if s["title"].lower() == title.lower()), None)


def render_card(card: dict, variables: dict) -> tuple[str, int]:
    """Renderiza el template: solo la sección Usuario lleva variables (convención
    de la flota: el Sistema tiene llaves de JSON literales). Lo que sale, redactado."""
    user = _section(card, "Usuario")
    if user is None:
        raise LLMHTTPError(422, "sin_seccion_usuario", detail=f"{card['repo']}/{card['id']} no tiene sección Usuario")
    try:
        rendered = user["body"].format(**variables)
    except KeyError as exc:
        raise LLMHTTPError(422, "falta_variable", detail=f"el template exige {exc}") from exc
    safe = redact(rendered)
    return safe.text, len(safe.findings)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class RenderRequest(BaseModel):
    variables: dict = {}


class DescribeRequest(BaseModel):
    repo: str
    prompt_id: str


def create_app(*, provider, fleet: list[dict], title: str = "prompt-hub", version: str = "0.1.0") -> FastAPI:
    client = build_client(provider)
    app = make_web_app(title, version, client=client, health_extra={"week": 17})
    index = {(c["repo"], c["id"]): c for c in fleet}

    def _card_or_404(repo: str, prompt_id: str) -> dict:
        card = index.get((repo, prompt_id))
        if card is None:
            raise LLMHTTPError(404, "prompt_no_encontrado", detail=f"{repo}/{prompt_id}")
        return card

    @app.get("/prompts", tags=["catálogo"])
    async def prompts():
        return {
            "count": len(fleet),
            "prompts": [
                {k: c[k] for k in ("repo", "id", "version", "schema", "lines", "approx_tokens")}
                for c in fleet
            ],
        }

    @app.get("/prompts/{repo}/{prompt_id}", tags=["catálogo"])
    async def prompt_detail(repo: str, prompt_id: str):
        return _card_or_404(repo, prompt_id)

    @app.post("/prompts/{repo}/{prompt_id}/render", tags=["catálogo"])
    async def prompt_render(repo: str, prompt_id: str, req: RenderRequest):
        card = _card_or_404(repo, prompt_id)
        rendered, redactions = render_card(card, req.variables)
        return {"repo": repo, "prompt_id": prompt_id, "rendered": rendered, "redactions": redactions}

    @app.post("/describe", tags=["catálogo"])
    async def describe(req: DescribeRequest):
        card = _card_or_404(req.repo, req.prompt_id)
        excerpt = "\n".join(s["body"] for s in card["sections"])[:EXCERPT_CHARS]
        return llm_complete(
            client,
            CompleteRequest(
                prompt_id=PROMPT_ID,
                prompt_version="0.1.0",
                variables={
                    "repo": req.repo,
                    "prompt_id": req.prompt_id,
                    "excerpt": sanitize_for_prompt(excerpt).text,
                },
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["prompt-hub", "week-17"],
            ),
        )

    return app


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["prompt-hub", "week-17"],
            )
        )

    return judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prompt-hub", description="Catálogo HTTP de los prompts de la flota (semana 17).")
    parser.add_argument("--repos", nargs="+", default=None, help="rutas explícitas (default: hermanos con core-consumer.yml)")
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "record", "replay-check"],
        help="scan imprime el inventario; record graba la cinta; replay-check valida sin LLM (D4).",
    )
    args = parser.parse_args(argv)
    repo_dirs = [Path(r) for r in args.repos] if args.repos else discover_fleet(ROOT.parent)

    if args.command == "scan":
        fleet = scan_fleet(repo_dirs)
        print(json.dumps(fleet, ensure_ascii=False, indent=2))
        return 0

    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    if args.command == "replay-check":
        provider = ReplayProvider(ROOT / "cassettes", record=False)
    else:
        provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
    client = build_client(provider)
    report = run(dataset, judge_for(client), mode="full", threshold=1.0)
    print(f"pass={report.passed}/{report.total} threshold_ok={report.threshold_ok}")
    return 0 if report.threshold_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
