"""Bind a real fitted QACD scorer; no mock provider or unfitted prior."""

from dataclasses import asdict

from qacd.pipeline import QACDPipeline, REFERENCE_FEATURE_NAMES
from qacd.providers import LLaVAProvider, RapidOCRProvider


class QACDScorer:
    def __init__(self, scorer_path: str, model_path: str):
        provider = LLaVAProvider(model_path=model_path, ocr_provider=RapidOCRProvider())
        self.pipeline = QACDPipeline.load(scorer_path, provider=provider)
        if (not self.pipeline.fitted_ or self.pipeline.assembler is not None
                or self.pipeline.feature_names != REFERENCE_FEATURE_NAMES
                or not self.pipeline.provenance.get("carries_model_evidence")
                or self.pipeline.provenance.get("provider_kind") != "llava"):
            raise ValueError("Online QACD requires a real fitted 48-feature scorer and matching provider")

    def score(self, question: str, answer: str, image: str) -> dict:
        return asdict(self.pipeline.score(question, answer, image))
