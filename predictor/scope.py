"""Scope extraction — the soft-signal half of the estimator.

Implements DESIGN.md steps 2 and 3. Everything here is deterministic, pure, and
sub-millisecond; no I/O, no model calls.

The governing idea, and the reason this file exists at all:

    Output length is determined by the SCOPE OF WORK requested, not by the length
    of the request.

We measured the alternative. Regressing output on input length gave R^2 = 0.009 --
input length explains under 1% of output variance, and two buckets were negatively
correlated. So every signal below reads what is being *asked for*, not how long the
asking took.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

Messages = List[Dict[str, Any]]
Payload = Union[str, Messages]

# ---------------------------------------------------------------------------
# Step 2 — explicit length instructions
# ---------------------------------------------------------------------------
# Where these fire they are the most accurate rule in the system, and coverage is
# the highest-value detail in the whole estimator: in testing, ONE missed hyphenated
# form ("two-sentence" vs "in two sentences") moved overall error from 84% to 192%
# MAPE. More than half of total error came from a single hyphen. Hence the
# deliberately permissive patterns below.

TOKENS_PER_WORD = 1.33      # property of English under BPE, not a fitted value
TOKENS_PER_SENTENCE = 25    # MEASURED: our "two sentence" outputs were 45/62/63/64
TOKENS_PER_PARAGRAPH = 100
TOKENS_PER_PAGE = 500

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple": 2,
    "a few": 3, "several": 4, "single": 1,
}

_UNIT_TOKENS = {
    "word": TOKENS_PER_WORD,
    "sentence": TOKENS_PER_SENTENCE,
    "line": 12.0,
    "bullet": 15.0,
    "point": 20.0,
    "item": 15.0,
    "paragraph": TOKENS_PER_PARAGRAPH,
    "page": TOKENS_PER_PAGE,
    "character": 0.25,
    "char": 0.25,
}

_UNITS = "|".join(sorted(_UNIT_TOKENS, key=len, reverse=True))
_QTY = r"(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

# "in 100 words", "100-word", "two sentences", "a 3 paragraph summary", "5 bullets"
_COUNT_RE = re.compile(
    rf"\b(?:in|within|about|around|roughly|approximately|under|over|at\s+least|"
    rf"no\s+more\s+than|no\s+less\s+than|max(?:imum)?(?:\s+of)?|min(?:imum)?(?:\s+of)?)?"
    rf"\s*{_QTY}[\s-]+({_UNITS})s?\b",
    re.IGNORECASE,
)

# Phrases that set a FLOOR rather than a ceiling — "at least 500 words" means the
# output will be 500+, so taking the number as an estimate under-predicts.
_FLOOR_RE = re.compile(r"\b(at\s+least|no\s+less\s+than|min(?:imum)?|more\s+than)\b", re.IGNORECASE)

# Qualitative cues — multipliers rather than absolute counts.
_TERSE = (
    "briefly", "be brief", "concise", "concisely", "tl;dr", "tldr", "in short",
    "short answer", "one word", "yes or no", "just the", "only the", "succinct",
    "keep it short", "as short as",
)
_VERBOSE = (
    "in detail", "detailed", "comprehensive", "comprehensively", "thorough",
    "thoroughly", "exhaustive", "in depth", "in-depth", "elaborate", "at length",
    "as much detail",
)
# NOTE: "step by step" deliberately does NOT appear here -- it is a CoT cue, and
# listing it in both places multiplied the same signal twice (x3.0 then x2.0),
# which produced a 157% over-prediction on a reasoning probe.
TERSE_SCALE = 0.4
VERBOSE_SCALE = 2.0

# "one word" / "yes or no" style answers are a floor of a few tokens, not a scale.
_ATOMIC = ("one word", "single word", "yes or no", "true or false", "just the number")
ATOMIC_TOKENS = 5


# ---------------------------------------------------------------------------
# Step 3A — additive task scopes
# ---------------------------------------------------------------------------
# Prompts are not exclusively one task. "Summarize this and write the code" is both,
# and exclusive buckets structurally cannot represent that. Addition can.

BASE_SCOPE = 150.0
SCOPE_CAP = 1500.0


@dataclass
class ScopeConfig:
    """Every tunable constant, in one place.

    They live here rather than as module literals so `predictor/optimize.py` can
    search over them without rewriting source, and so a fitted configuration can be
    serialised, diffed, reviewed and rolled back. DESIGN.md §10 records which of
    these were measured and which are guesses -- almost all are guesses, which is
    exactly why they need to be searchable.
    """

    base_scope: float = BASE_SCOPE
    scope_cap: float = SCOPE_CAP
    task_summary: float = 250.0
    task_code: float = 500.0
    task_extract: float = 100.0
    task_search: float = 400.0
    high_intensity: float = 1.5
    low_intensity: float = 0.6
    cot: float = 3.0
    terse: float = 0.4
    verbose: float = 2.0
    instruction_low: float = 0.5
    instruction_high: float = 1.5
    instruction_pivot: float = 200.0
    tokens_per_sentence: float = 25.0
    tokens_per_word: float = 1.33
    imperative: float = 1.4

    def replace(self, **kw) -> "ScopeConfig":
        from dataclasses import replace as _r
        return _r(self, **kw)


DEFAULT_CONFIG = ScopeConfig()

TASK_SCOPES: Dict[str, Tuple[float, Tuple[str, ...]]] = {
    "summary": (250.0, (
        "summarize", "summarise", "summary", "tldr", "tl;dr", "condense",
        "overview", "recap", "key points", "gist",
    )),
    "code": (500.0, (
        "code", "function", "class", "debug", "implement", "implementation",
        "script", "module", "refactor", "unit test", "regex", "sql", "query",
        "python", "javascript", "typescript", "api endpoint",
    )),
    "extract": (100.0, (
        "extract", "json", "parse", "schema", "structured output", "csv",
        "table of", "key-value", "fields",
    )),
    "search": (400.0, (
        "search the web", "browse", "latest news", "look up", "current events",
        "cite sources", "with citations",
    )),
}


# ---------------------------------------------------------------------------
# Step 3B / 3C — multipliers
# ---------------------------------------------------------------------------

HIGH_INTENSITY = (
    "rewrite", "refactor", "redesign", "entire", "whole", "build", "create",
    "from scratch", "complete", "full", "every", "all of", "comprehensive",
    "end to end", "end-to-end", "production-ready",
)
LOW_INTENSITY = (
    "fix", "tweak", "rename", "adjust", "correct", "patch", "typo", "one line",
    "small change", "minor",
)
HIGH_INTENSITY_SCALE = 1.5
LOW_INTENSITY_SCALE = 0.6

# The strongest single predictor found on 522 real WildChat prompts:
# corr(imperative_start, output_tokens) = +0.378, versus +0.28 for anything else we
# tested including input length. A prompt that OPENS with a generation verb is asking
# for something to be produced; one that opens with a question usually wants an answer.
# Cheap, and it fires on real phrasing rather than on keywords we invented.
IMPERATIVE_START = (
    "write", "create", "make", "generate", "give", "list", "explain", "describe",
    "compose", "draft", "design", "build", "produce", "summarize", "translate",
)

COT_CUES = (
    "think step by step", "step-by-step", "step by step", "reason through",
    "reasoning", "show your work", "justify", "walk me through", "chain of thought",
    "explain your reasoning",
)
COT_SCALE = 3.0

# Models that emit reasoning tokens regardless of the prompt. Those tokens are billed
# as output, so the multiplier has to key on the MODEL, not on prompt text -- a gap
# the prompt-only version of this rule silently missed.
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5-thinking", "deepseek-r")
REASONING_MODEL_SCALE = 3.0


def _compile(cues: Tuple[str, ...]) -> re.Pattern:
    """Word-boundary alternation over a cue list.

    Naive substring containment is the bug that made the first version of this file
    score 975% error on a JSON probe: "create" matched inside "created_at", and
    "code" matched inside "status codes", inflating an extraction request into a
    from-scratch code-generation request. buckets.py already got this right; this
    file did not, and the measurement caught it.
    """
    alternation = "|".join(re.escape(c) for c in sorted(cues, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


_TASK_PATTERNS: Dict[str, Tuple[float, re.Pattern]] = {
    name: (weight, _compile(cues)) for name, (weight, cues) in TASK_SCOPES.items()
}
_HIGH_RE = _compile(HIGH_INTENSITY)
_LOW_RE = _compile(LOW_INTENSITY)
_COT_RE = _compile(COT_CUES)
_TERSE_RE = _compile(_TERSE)
_VERBOSE_RE = _compile(_VERBOSE)
_ATOMIC_RE = _compile(_ATOMIC)
_IMPERATIVE_RE = re.compile(r"^\s*(?:please\s+)?(?:" +
                            "|".join(IMPERATIVE_START) + r")\b", re.IGNORECASE)


def imperative_scale(text: str, cfg: "ScopeConfig" = None) -> float:
    """Does the prompt OPEN with a generation verb? See IMPERATIVE_START."""
    cfg = cfg or DEFAULT_CONFIG
    return cfg.imperative if _IMPERATIVE_RE.match(text) else 1.0


def _text_of(payload: Payload) -> Tuple[str, str]:
    """Use standing system constraints and the latest user request, not stale turns."""
    if isinstance(payload, str):
        return payload.lower(), payload.lower()
    def content(message: dict) -> str:
        value = message.get("content", "")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(part.get("text", "") for part in value
                            if isinstance(part, dict) and isinstance(part.get("text"), str))
        return ""

    standing = [content(m) for m in payload
                if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    users = [content(m) for m in payload
             if isinstance(m, dict) and m.get("role") == "user"]
    last = users[-1] if users else next((content(m) for m in reversed(payload)
                                        if isinstance(m, dict) and content(m)), "")
    return " ".join([*standing, last]).lower(), last.lower()


def parse_length_instruction(text: str, cfg: "ScopeConfig" = None) -> Optional[Tuple[float, str]]:
    """Step 2. Returns (tokens, rule) when the prompt states a length, else None."""
    cfg = cfg or DEFAULT_CONFIG
    if _ATOMIC_RE.search(text):
        return float(ATOMIC_TOKENS), "atomic"

    match = _COUNT_RE.search(text)
    if match:
        raw_qty, unit = match.group(1), match.group(2).lower()
        qty = float(raw_qty) if raw_qty.isdigit() else float(_NUMBER_WORDS.get(raw_qty, 0))
        if qty > 0:
            per_unit = {"sentence": cfg.tokens_per_sentence,
                        "word": cfg.tokens_per_word}.get(unit, _UNIT_TOKENS[unit])
            tokens = qty * per_unit
            # "at least N" is a floor: the model will produce N or more, so treating
            # N as the estimate guarantees under-prediction. Inflate instead.
            window = text[max(0, match.start() - 30):match.end()]
            if _FLOOR_RE.search(window):
                return tokens * 1.5, f"{unit}s_floor"
            return tokens, f"{unit}s"
    return None


def qualitative_scale(text: str, cfg: "ScopeConfig" = None) -> float:
    """Terse/verbose cues, applied as a multiplier rather than an absolute count."""
    cfg = cfg or DEFAULT_CONFIG
    if _TERSE_RE.search(text):
        return cfg.terse
    if _VERBOSE_RE.search(text):
        return cfg.verbose
    return 1.0


def task_scope(text: str, cfg: "ScopeConfig" = None) -> Tuple[float, List[str]]:
    """Step 3A. Base plus every detected task, capped before multipliers apply."""
    cfg = cfg or DEFAULT_CONFIG
    weights = {"summary": cfg.task_summary, "code": cfg.task_code,
               "extract": cfg.task_extract, "search": cfg.task_search}
    scope = cfg.base_scope
    detected: List[str] = []
    for name, (_, pattern) in _TASK_PATTERNS.items():
        if pattern.search(text):
            scope += weights.get(name, 0.0)
            detected.append(name)
    return min(scope, cfg.scope_cap), detected


def verb_scale(text: str, cfg: "ScopeConfig" = None) -> float:
    """Step 3B. 'Change one button' versus 'change the entire website'."""
    cfg = cfg or DEFAULT_CONFIG
    if _HIGH_RE.search(text):
        return cfg.high_intensity
    if _LOW_RE.search(text):
        return cfg.low_intensity
    return 1.0


def cot_scale(text: str, model: str = "", cfg: "ScopeConfig" = None) -> float:
    """Step 3C. Reasoning dumps intermediate thought into billed output tokens."""
    cfg = cfg or DEFAULT_CONFIG
    lowered = (model or "").lower()
    if any(lowered.startswith(p) for p in REASONING_MODEL_PREFIXES):
        return cfg.cot
    return cfg.cot if _COT_RE.search(text) else 1.0


def instruction_scale(last_message: str, cfg: "ScopeConfig" = None) -> float:
    """Step 3D. A message that is mostly pasted material wants a targeted edit; a
    message that is mostly instruction wants generation.

    Honest description of what this computes: it reduces to
    ``clamp(400 / len(message), 0.5, 1.5)`` -- a step function on message length,
    not a true instruction/context ratio. Under ~267 chars it returns 1.5, over 800
    it returns 0.5. Tested marginally better than splitting on real paragraph
    boundaries (84% vs 87% MAPE at n=15), so the simple form ships. Known failure:
    a long-but-genuine instruction is penalised as though it were payload.
    """
    cfg = cfg or DEFAULT_CONFIG
    length = len(last_message)
    if length == 0:
        return 1.0
    ratio = min(cfg.instruction_pivot, length) / length
    return max(cfg.instruction_low, min(cfg.instruction_high, ratio * 2.0))


def estimate(payload: Payload, model: str = "",
             cfg: "ScopeConfig" = None) -> Tuple[float, str, List[str]]:
    """Steps 2-3 combined. Returns (scope_tokens, rule, detected_tasks).

    Does NOT apply the safety buffer, the history correction, or the max_tokens
    clamp -- those are steps 4-6 and live in engine.py.
    """
    cfg = cfg or DEFAULT_CONFIG
    full, last = _text_of(payload)

    explicit = parse_length_instruction(full, cfg)
    if explicit is not None:
        return explicit[0], explicit[1], []

    scope, detected = task_scope(full, cfg)
    scope *= verb_scale(full, cfg)
    scope *= cot_scale(full, model, cfg)
    scope *= qualitative_scale(full, cfg)
    scope *= instruction_scale(last, cfg)
    scope *= imperative_scale(last, cfg)
    return scope, "stacked", detected
