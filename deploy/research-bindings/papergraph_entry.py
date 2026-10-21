"""Resource owner for original PaperGraph functions, without rubric leakage.

Copy into papergraph_demo/ before intake. Full verification retains the
original extraction and complete loop. The finer methods are for an explicitly
declared new TaskSpec; they do not grant permission to omit the original loop.
"""

import json
import os
from pathlib import Path

from src.model_client import ModelConfig, OpenAICompatClient
from src.claim_extractor import extract_atomic_claims, verifiable_claims
from src.claim_verifier import verify_global_claims, _verify_one_claim, claim_verification_enabled
from src.kc_retriever import retrieve_kc_and_evidence


class PaperGraphVerifier:
    def __init__(self, kc_bank_path: str, base_url: str, llm_model: str,
                 embed_base_url: str, embed_model: str):
        self.kc_bank = json.loads(Path(kc_bank_path).read_text(encoding="utf-8"))
        self.client = OpenAICompatClient(ModelConfig(api_key=os.environ.get("PAPERGRAPH_API_KEY", ""),
            base_url=base_url, llm_model=llm_model, embed_api_key=os.environ.get("EMBED_API_KEY", ""),
            embed_base_url=embed_base_url, embed_model=embed_model))

    def verify(self, turn: dict) -> list:
        if not claim_verification_enabled():
            raise ValueError("CLAIM_VERIFY_ENABLED must be true for an active verification action")
        return verify_global_claims(turn, self.kc_bank, self.client)

    def extract(self, turn: dict) -> list:
        return verifiable_claims(extract_atomic_claims(turn, self.client))

    def verify_claim(self, claim: dict) -> dict:
        retrieval = retrieve_kc_and_evidence(claim, self.kc_bank, self.client)
        return _verify_one_claim(claim, retrieval, self.client)
