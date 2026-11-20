from __future__ import annotations

from pydantic import BaseModel


class PromptCard(BaseModel):
    """Ficha de catálogo de un prompt (`prompt-card-v1`)."""

    repo: str
    prompt_id: str
    purpose: str
    when_to_use: str
    inputs: list[str] = []
    output: str
