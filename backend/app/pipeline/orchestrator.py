"""Adaptive extraction orchestration.

    EASY       -> tier 1 rules            (free)
    MEDIUM     -> tier 2 OCR + small LLM  (fractions of a cent)
    DIFFICULT  -> tier 3 document AI      (Azure DI if configured, else Claude vision)
    any failure-> next tier up ... tier 4 fallback (top model, sees prior errors)
    still failing -> best attempt goes to human review

Pure logic, no database: returns every attempt so the caller can persist the audit trail.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.pipeline.engines.azure_di import AzureDocumentIntelligenceEngine
from app.pipeline.engines.base import Engine, EngineResult, EngineUnavailable
from app.pipeline.engines.claude import FallbackEngine, OcrLlmEngine, VisionEngine
from app.pipeline.engines.rules import RulesEngine
from app.pipeline.preprocess import DocumentContext
from app.pipeline.validation import ValidationReport, validate
from app.schemas import Complexity, DocType

log = logging.getLogger(__name__)

START_TIER = {Complexity.EASY: 1, Complexity.MEDIUM: 2, Complexity.DIFFICULT: 3}


@dataclass
class Attempt:
    engine: str
    tier: int
    duration_ms: int
    result: EngineResult | None = None
    validation: ValidationReport | None = None
    error: str | None = None
    skipped: bool = False

    @property
    def score(self) -> float:
        return self.validation.score if self.validation else -1.0

    @property
    def cost(self) -> float:
        return self.result.cost_usd if self.result else 0.0


@dataclass
class Outcome:
    attempts: list[Attempt] = field(default_factory=list)
    best: Attempt | None = None
    accepted: bool = False

    @property
    def total_cost(self) -> float:
        return round(sum(a.cost for a in self.attempts), 6)


def default_ladder() -> list[Engine]:
    azure = AzureDocumentIntelligenceEngine()
    tier3: Engine = azure if azure.available() else VisionEngine()
    return [RulesEngine(), OcrLlmEngine(), tier3, FallbackEngine()]


class Orchestrator:
    def __init__(self, ladder: list[Engine] | None = None):
        self.ladder = sorted(ladder or default_ladder(), key=lambda e: e.tier)

    def run(self, ctx: DocumentContext, complexity: Complexity, doc_type: DocType | None) -> Outcome:
        settings = get_settings()
        start = START_TIER[complexity]
        # A doc with no text at all can't be handled by the rules engine regardless of score.
        if start == 1 and not ctx.has_usable_text:
            start = 2
        outcome = Outcome()

        for engine in (e for e in self.ladder if e.tier >= start):
            if outcome.total_cost >= settings.max_cost_per_document_usd:
                log.warning("cost cap reached for %s, stopping escalation", ctx.filename)
                break
            if not engine.available():
                outcome.attempts.append(Attempt(engine.name, engine.tier, 0, error="engine not configured", skipped=True))
                continue
            if isinstance(engine, FallbackEngine) and outcome.best and outcome.best.result:
                engine.previous_result = outcome.best.result.document.model_dump(mode="json")
                engine.previous_issues = outcome.best.validation.messages() if outcome.best.validation else []

            t0 = time.monotonic()
            attempt = Attempt(engine.name, engine.tier, 0)
            try:
                result = engine.extract(ctx, doc_type)
                if doc_type and result.document.document_type == DocType.OTHER:
                    result.document.document_type = doc_type
                attempt.result = result
                attempt.validation = validate(result.document, result.engine_confidence)
            except EngineUnavailable as exc:
                attempt.error, attempt.skipped = str(exc), True
            except Exception as exc:  # noqa: BLE001 - any engine failure means escalate
                log.exception("engine %s failed on %s", engine.name, ctx.filename)
                attempt.error = f"{type(exc).__name__}: {exc}"
            attempt.duration_ms = int((time.monotonic() - t0) * 1000)
            outcome.attempts.append(attempt)

            if attempt.validation and (outcome.best is None or attempt.score > outcome.best.score):
                outcome.best = attempt
            if attempt.validation and attempt.validation.passed:
                outcome.accepted = True
                outcome.best = attempt
                break
        return outcome
