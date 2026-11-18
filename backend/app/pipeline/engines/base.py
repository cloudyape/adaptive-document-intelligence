from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.pipeline.preprocess import DocumentContext
from app.schemas import DocType, ExtractedDocument


@dataclass
class EngineResult:
    document: ExtractedDocument
    engine_confidence: float = 0.5  # engine's own self-reported confidence, 0-1
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None
    warnings: list[str] = field(default_factory=list)


class EngineUnavailable(RuntimeError):
    """Engine can't run in this deployment (missing credentials/binary). Orchestrator skips it."""


class Engine(Protocol):
    name: str
    tier: int  # 1 = cheapest

    def available(self) -> bool: ...

    def extract(self, ctx: DocumentContext, doc_type_hint: DocType | None = None) -> EngineResult: ...
