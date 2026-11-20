"""Feature-flag registry of the SCI reference benchmark.

Each flag maps an experiment to the official Green Software Foundation (GSF)
pattern(s) it implements. The official metadata (name, lifecycle phase,
Priority Score, link) is NOT hard-coded: it is resolved at runtime against the
GSF catalogue bundled with ``roche_green`` (``roche_green.suggest.load_patterns_db``).

Resolution uses an ordered list of lookup rules per flag; the first rule that
matches at least one catalogue row wins (ties are broken by highest Priority
Score). This keeps the mapping robust when the catalogue is updated: e.g. if a
dedicated "Optimize prompts" pattern is added, ``prompt_compression`` will pick
it up automatically without code changes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any


# --------------------------------------------------------------------------- lookup rules
@dataclass(frozen=True)
class LookupRule:
    kind: str                # "exact" | "name" | "desc"
    terms: tuple[str, ...]   # all terms must match (case-insensitive)

    def matches(self, record: dict[str, Any]) -> bool:
        name = str(record.get("Pattern Name", "")).casefold()
        desc = str(record.get("Description", "")).casefold()
        terms = [t.casefold() for t in self.terms]
        if self.kind == "exact":
            return name.strip() == terms[0]
        haystack = name if self.kind == "name" else desc
        return all(t in haystack for t in terms)

    def __str__(self) -> str:
        label = {"exact": "name ==", "name": "name contains", "desc": "description contains"}[self.kind]
        return f"{label} {' + '.join(repr(t) for t in self.terms)}"


def exact(name: str) -> LookupRule:
    return LookupRule("exact", (name,))


def name_has(*terms: str) -> LookupRule:
    return LookupRule("name", terms)


def desc_has(*terms: str) -> LookupRule:
    return LookupRule("desc", terms)


@dataclass(frozen=True)
class PatternFlag:
    flag: str
    category: str                        # api | batch | ai  (shares the functional unit R)
    is_baseline: bool
    lookup: tuple[LookupRule, ...] = ()  # ordered: first matching rule -> primary GSF pattern
    related: tuple[str, ...] = ()        # exact names of secondary GSF patterns
    rationale: str = ""


FLAGS: dict[str, PatternFlag] = {f.flag: f for f in (
    # ---------------------------------------------------------------- API (R=1000)
    PatternFlag("api_baseline", "api", True,
                rationale="Anti-pattern: SELECT * without index/pagination, no cache, pretty-printed JSON."),
    PatternFlag("cache", "api", False, (exact("Cache static data"),),
                rationale="Recurrent cohort queries are served from an in-memory LRU cache."),
    PatternFlag("pagination", "api", False, (exact("Reduce transmitted data"),),
                ("Avoid tracking unnecessary data",),
                "Filter early in SQL, project only needed columns, LIMIT a page, compact JSON."),
    # ---------------------------------------------------------------- Batch (R=10000)
    PatternFlag("batch_baseline", "batch", True,
                rationale="Anti-pattern: commit() per record on a carbon-intensive grid (DEU)."),
    PatternFlag("batching", "batch", False, (exact("Queue non-urgent processing requests"),),
                ("Keep request counts low",),
                "Records grouped and loaded with executemany in a single transaction."),
    PatternFlag("carbon_aware", "batch", False,
                (exact("Use carbon-aware scheduling and region selection for AI workloads"),),
                ("Time-shift Kubernetes cron jobs",),
                "Batching + time/region shift to the greenest forecast window (SWE)."),
    # ---------------------------------------------------------------- AI (R=100)
    PatternFlag("ai_baseline", "ai", True,
                rationale="Oversized fp16 70B model with a monolithic, uncached prompt."),
    PatternFlag("model_rightsizing", "ai", False, (exact("Use right-sized and energy-efficient AI models"),),
                ("Select efficient accelerators and instance types for AI workloads",),
                "Quantized 8B int8 model sized to the extraction task."),
    PatternFlag("prompt_compression", "ai", False,
                (
                    exact("Optimize prompts"),                 # dedicated prompt pattern, if present
                    name_has("prompt"),                        # any pattern about prompts
                    name_has("caching for repetitive AI"),     # AI response/prefix caching
                    desc_has("prompt", "caching"),             # AI pattern describing prompt caching
                ),
                ("Compress transmitted data",),
                "Compressed prompt + shared prefix reused from the KV-cache (prompt/token caching)."),
)}

ANTI_PATTERN_LABEL = "Baseline (anti-pattern)"


@dataclass
class GsfMetadata:
    gsf_pattern_name: str
    sw_lifecycle: str
    priority_score: float | None
    gsf_link: str
    persona: str = ""
    description: str = ""
    matched_by: str = ""
    related_patterns: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _catalogue() -> tuple[dict[str, Any], ...]:
    """Load the GSF catalogue once; degrade gracefully if it cannot be read."""
    try:
        from roche_green.suggest import load_patterns_db
        df = load_patterns_db()
    except (Exception, SystemExit) as exc:  # load_patterns_db() calls sys.exit on errors
        print(f"[patterns] WARNING: GSF catalogue unavailable ({exc!r}); metadata left empty.")
        return ()
    return tuple(r for r in df.to_dict(orient="records") if str(r.get("Pattern Name", "")).strip())


def _clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""
    return str(value).strip()


def _score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return None if score != score else score


def _resolve(rules: tuple[LookupRule, ...]) -> tuple[dict[str, Any] | None, str]:
    rows = _catalogue()
    for rule in rules:
        hits = [r for r in rows if rule.matches(r)]
        if hits:
            best = max(hits, key=lambda r: _score(r.get("Priority Score")) or 0.0)
            return best, str(rule)
    return None, ""


def gsf_metadata(flag: str) -> GsfMetadata:
    spec = FLAGS[flag]
    if spec.is_baseline:
        return GsfMetadata(ANTI_PATTERN_LABEL, "N/A", None, "", description=spec.rationale)
    primary, matched_by = _resolve(spec.lookup)
    related = [name for name in spec.related if _resolve((exact(name),))[0] is not None]
    if primary is None:
        return GsfMetadata(f"(no catalogue match for {flag})", "", None, "",
                           description=spec.rationale, related_patterns=related)
    return GsfMetadata(
        gsf_pattern_name=_clean(primary.get("Pattern Name")),
        sw_lifecycle=_clean(primary.get("SW Lifecycle")),
        priority_score=_score(primary.get("Priority Score")),
        gsf_link=_clean(primary.get("GSF Link")),
        persona=_clean(primary.get("Persona")),
        description=_clean(primary.get("Description")),
        matched_by=matched_by,
        related_patterns=related,
    )


def enrich(flag: str) -> dict[str, Any]:
    """Flag spec + official GSF metadata as a flat, JSON-serializable dict."""
    spec = FLAGS[flag]
    return {"flag": flag, "category": spec.category, "is_baseline": spec.is_baseline,
            "rationale": spec.rationale, **asdict(gsf_metadata(flag))}


def baseline_of(category: str) -> str:
    return next(f.flag for f in FLAGS.values() if f.category == category and f.is_baseline)


if __name__ == "__main__":
    for name in FLAGS:
        m = gsf_metadata(name)
        print(f"{name:<20} -> {m.gsf_pattern_name} | {m.sw_lifecycle} | {m.priority_score} | "
              f"{m.gsf_link} | matched by: {m.matched_by or '-'}")
