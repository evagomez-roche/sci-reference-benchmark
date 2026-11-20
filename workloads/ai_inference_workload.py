"""AI inference workload: R = 100 ``ai_inferences`` (SCI for AI + CLEER_v2).

Use case: pharmacovigilance extraction of adverse events from synthetic clinical
notes (``fixtures/clinical_notes.json``). No external API is called: the LLM is
replaced by a *deterministic* local simulator so the experiment is reproducible
on a laptop or on an air-gapped sHPC node.

How the carbon is accounted
---------------------------
* Local energy (prompt building, tokenisation, simulated forward passes) is
  measured by CodeCarbon inside ``GreenLogger``.
* Remote accelerator energy + embodied carbon are estimated by the SDK with the
  ``CLEER_v2`` model from the declared token counts (``is_ai=True``), applying
  the 85% discount to ``cached_tokens``.

The simulator builds prompts with EXACTLY the token budget declared to
``GreenLogger`` and the runner asserts it after each run, so the CLEER inputs
are not just declared: they are reproduced by the workload.

Variants (per inference; totals = value x R)
--------------------------------------------
* ``ai_baseline``        ``llama-3-70b-fp16``, 1800 prompt / 0 cached / 350 completion.
* ``model_rightsizing``  GSF "Use right-sized and energy-efficient AI models":
  ``llama-3-8b-int8-quantized``, 1800 prompt / 0 cached / 250 completion.
* ``prompt_compression`` GSF prompt/token-caching pattern (resolved at runtime by
  ``patterns.py`` keyword lookup) + "Compress transmitted data": prompt compression
  and KV-cache prefix reuse on the same 70B model to isolate the effect,
  320 fresh prompt / 1100 cached prefix / 180 completion.

System boundary: loading fixtures and warming the shared prompt prefix in the
KV-cache (a long-running server would already have it) are outside; the R
inferences are measured.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .base import Variant, WorkloadCategory

CATEGORY_NAME = "ai"
FUNCTIONAL_UNIT_NAME = "ai_inferences"
R_AI = 100
PROVIDER = "Local Simulation"
FIXTURES_FILE = Path(__file__).resolve().parent / "fixtures" / "clinical_notes.json"
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    params_billion: float
    bytes_per_param: float  # fp16 = 2, int8 = 1

    @property
    def work_per_token(self) -> int:
        """Relative compute per token (~ weights touched), used by the local simulator."""
        return max(1, round(self.params_billion * self.bytes_per_param / 10))


@dataclass(frozen=True)
class TokenProfile:
    prompt: int      # fresh (non-cached) input tokens per inference
    cached: int      # input tokens served from the KV-cache per inference
    completion: int  # generated tokens per inference


LLAMA_70B_FP16 = ModelProfile("llama-3-70b-fp16", 70, 2.0)
LLAMA_8B_INT8 = ModelProfile("llama-3-8b-int8-quantized", 8, 1.0)

PROFILES: dict[str, tuple[ModelProfile, TokenProfile]] = {
    "ai_baseline": (LLAMA_70B_FP16, TokenProfile(prompt=1800, cached=0, completion=350)),
    "model_rightsizing": (LLAMA_8B_INT8, TokenProfile(prompt=1800, cached=0, completion=250)),
    "prompt_compression": (LLAMA_70B_FP16, TokenProfile(prompt=320, cached=1100, completion=180)),
}

_STOPWORDS = frozenset(
    "a an the and or of to in on at for with by from as is was were be been his her their "
    "this that these those it its any all no not after before during within without".split()
)


# --------------------------------------------------------------------------- tokenizer
def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _fit(tokens: list[str], budget: int, filler: list[str]) -> list[str]:
    """Truncate or deterministically pad a token list to exactly ``budget`` tokens."""
    if len(tokens) >= budget:
        return tokens[:budget]
    out = list(tokens)
    i = 0
    while len(out) < budget:
        out.append(filler[i % len(filler)])
        i += 1
    return out


# --------------------------------------------------------------------------- simulator
class DeterministicLLM:
    """Toy decoder: cost per token scales with model size; output is seed-stable."""

    def __init__(self, model: ModelProfile, vocab: list[str]):
        self.model = model
        self.vocab = vocab
        self.kv_cache: dict[str, int] = {}
        self.tokens_computed = 0

    def _forward(self, state: int, token: str) -> int:
        h = state ^ (hash_token(token))
        for _ in range(self.model.work_per_token):
            h = (h * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        self.tokens_computed += 1
        return h

    def prefill(self, tokens: list[str], state: int = 0) -> int:
        for tok in tokens:
            state = self._forward(state, tok)
        return state

    def warm_prefix(self, prefix: list[str]) -> str:
        key = hashlib.sha256(" ".join(prefix).encode()).hexdigest()
        self.kv_cache[key] = self.prefill(prefix)
        return key

    def generate(self, prompt: list[str], cached_prefix_key: str | None,
                 max_new_tokens: int) -> list[str]:
        state = self.kv_cache[cached_prefix_key] if cached_prefix_key else 0
        state = self.prefill(prompt, state)
        out: list[str] = []
        for _ in range(max_new_tokens):
            tok = self.vocab[state % len(self.vocab)]
            out.append(tok)
            state = self._forward(state, tok)
        return out


_HASH_CACHE: dict[str, int] = {}


def hash_token(token: str) -> int:
    h = _HASH_CACHE.get(token)
    if h is None:
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
        _HASH_CACHE[token] = h
    return h


# --------------------------------------------------------------------------- prompts
def compress_note(note: str) -> list[str]:
    """Prompt compression: drop stop-words/punctuation and duplicated tokens."""
    seen: set[str] = set()
    out = []
    for tok in tokenize(note):
        low = tok.lower()
        if low in _STOPWORDS or not tok.isalnum() or low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return out


@dataclass
class AiState:
    flag: str
    model: ModelProfile
    tokens: TokenProfile
    llm: DeterministicLLM
    prompts: list[list[str]]
    prefix_key: str | None
    r: int


def _make_prepare(flag: str):
    def prepare(r: int) -> Iterator[AiState]:
        model, tokens = PROFILES[flag]
        fx = json.loads(FIXTURES_FILE.read_text(encoding="utf-8"))
        instructions = tokenize(" ".join([fx["system_prompt"], fx["task"], fx["output_schema"]]))
        examples = tokenize(" ".join(fx["few_shot_examples"]))
        filler = tokenize(" ".join(fx["notes"]))  # deterministic context padding
        vocab = sorted(set(instructions + examples + filler))
        llm = DeterministicLLM(model, vocab)
        prompts: list[list[str]] = []
        prefix_key = None

        if tokens.cached:
            # Stable shared prefix (instructions + few-shot) -> served by the KV-cache.
            prefix = _fit(instructions + examples, tokens.cached, filler)
            prefix_key = llm.warm_prefix(prefix)  # warm-up outside the system boundary
            llm.tokens_computed = 0
            for i in range(r):
                note = compress_note(fx["notes"][i % len(fx["notes"])])
                prompts.append(_fit(note + [f"case_{i}"], tokens.prompt, filler))
        else:
            # Monolithic prompt rebuilt from scratch for each request.
            for i in range(r):
                note = tokenize(fx["notes"][i % len(fx["notes"])]) + [f"case_{i}"]
                prompts.append(_fit(instructions + examples + note, tokens.prompt, filler))
        yield AiState(flag=flag, model=model, tokens=tokens, llm=llm, prompts=prompts,
                      prefix_key=prefix_key, r=r)
    return contextmanager(prepare)


def _execute(state: AiState) -> dict[str, Any]:
    prompt_total = cached_total = completion_total = 0
    digest = hashlib.sha256()
    for prompt in state.prompts:
        completion = state.llm.generate(prompt, state.prefix_key, state.tokens.completion)
        prompt_total += len(prompt)
        cached_total += state.tokens.cached if state.prefix_key else 0
        completion_total += len(completion)
        digest.update(" ".join(completion).encode())
    return {
        "units_processed": len(state.prompts),
        "model_name": state.model.name,
        "prompt_tokens": prompt_total,
        "cached_tokens": cached_total,
        "completion_tokens": completion_total,
        "tokens_computed_locally": state.llm.tokens_computed,
        "simulated_compute_units": state.llm.tokens_computed * state.model.work_per_token,
        "output_digest": digest.hexdigest()[:16],
    }


def _logger_kwargs(state: AiState) -> dict[str, Any]:
    return {
        "is_ai": True,
        "model_name": state.model.name,
        "prompt_tokens": state.tokens.prompt * state.r,
        "cached_tokens": state.tokens.cached * state.r,
        "completion_tokens": state.tokens.completion * state.r,
        "provider": PROVIDER,
    }


_SUMMARIES = {
    "ai_baseline": "llama-3-70b-fp16, 1800 prompt / 0 cached / 350 completion tokens per inference",
    "model_rightsizing": "llama-3-8b-int8-quantized, 1800 prompt / 0 cached / 250 completion",
    "prompt_compression": "70B + compressed prompt + KV-cache prefix: 320 prompt / 1100 cached / 180 completion",
}

CATEGORY = WorkloadCategory(
    name=CATEGORY_NAME,
    functional_unit_name=FUNCTIONAL_UNIT_NAME,
    functional_units=R_AI,
    baseline_flag="ai_baseline",
    variants={
        flag: Variant(flag=flag, category=CATEGORY_NAME, is_baseline=(flag == "ai_baseline"),
                      summary=_SUMMARIES[flag], prepare=_make_prepare(flag), execute=_execute,
                      logger_kwargs=_logger_kwargs)
        for flag in PROFILES
    },
)
