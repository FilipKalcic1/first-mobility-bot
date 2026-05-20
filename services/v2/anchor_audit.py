"""Anchor quality audit — proactive analysis of `config/tool_data.json`
anchors. Identifies anchors that match anti-patterns we know hurt
routing accuracy (Filip 2026-05-17 spec from manual review of POST
tools).

Pure analysis (no I/O). Runner `scripts/audit_anchor_quality.py` does
the file load + optional Azure embedding + markdown render.

5 flag types:

  VERBOSE              — > 10 riječi (loš signal-to-noise)
  PARAPHRASE_INTENT    — kosinus s intent_summary > 0.85 (redundantan)
  DOC_STYLE            — sadrži "Korisnik želi", "Mogu li", itd.
                         (driver ne tipka tako u WhatsApp-u)
  CAPITALIZED_FORMAL   — počinje velikim slovom + glagolska imperativna
                         forma (driver tipiše lowercase u WhatsApp-u)
  REPETITIVE_VERBS     — tool-level flag: isti glagol-stem 3+ puta
                         (signal anchori su parafraze jedan drugog)
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field


# Anti-pattern flag names — used in report rendering.
FLAG_VERBOSE = "VERBOSE"
FLAG_PARAPHRASE = "PARAPHRASE_INTENT"
FLAG_DOC_STYLE = "DOC_STYLE"
FLAG_CAPITALIZED_FORMAL = "CAPITALIZED_FORMAL"
FLAG_REPETITIVE_VERBS = "REPETITIVE_VERBS"


VERBOSE_THRESHOLD_WORDS = 10
PARAPHRASE_COSINE_THRESHOLD = 0.85
REPETITIVE_VERB_THRESHOLD = 3


# Croatian doc-style phrases — drivers don't use these in WhatsApp.
# Either present in third-person ("Korisnik želi...") or as polite
# questions that LLM defaulted to during anchor generation.
_DOC_STYLE_RE = re.compile(
    r"\b(?:"
    r"korisnik\s+(?:želi|zeli|hoće|hoce|treba|mora|može|moze)"
    r"|user\s+(?:wants|needs|asks)"
    r"|(?:mogu|možeš|moze|mozes|možemo|mozemo)\s+li"
    r"|(?:što|sto)\s+(?:trebam|treba|moram|mogu)"
    r"|imam\s+(?:za|nešto|nesto)"
    r"|treba\s+mi\s+opcija"
    r"|kako\s+(?:mogu|da|bih|treba|trebam)"
    r"|želim\s+(?:dodati|izmijeniti|izbrisati|napraviti)"
    r"|zelim\s+(?:dodati|izmijeniti|izbrisati|napraviti)"
    r"|molim\s+te"
    r"|molim\s+(?:vas|prikaz|prikaži|prikazi)"
    r")\b",
    re.IGNORECASE,
)


# Croatian imperative endings — flags first word "Dodaj" / "Ažuriraj" /
# "Učitaj" / "Postavi" etc., which are formal command form drivers
# rarely use ("dodaj" is fine; "Dodaj" is doc style).
_IMPERATIVE_SUFFIXES = ("aj", "ji", "ši", "đi", "i", "uj", "vi")


# Croatian verb stem extraction — for repetitive-verb detection. We
# take first 4-5 chars of first word as proxy for stem (crude but
# good enough for spotting "Ažuriraj X", "Ažuriraj Y", "Ažuriraj Z").
_VERB_STEM_LEN = 5


@dataclass(frozen=True)
class AnchorFlag:
    """One flag against one anchor."""
    anchor_index: int
    anchor_text: str
    flag: str
    detail: str = ""


@dataclass(frozen=True)
class ToolAuditResult:
    tool_id: str
    method: str
    intent_summary: str
    anchor_count: int
    flagged_anchors: int            # how many anchors had >=1 flag
    flags: list = field(default_factory=list)  # list[AnchorFlag]
    tool_level_flags: list = field(default_factory=list)  # ["REPETITIVE_VERBS:ažur"]

    @property
    def total_flag_count(self) -> int:
        return len(self.flags) + len(self.tool_level_flags)


# --------------------------------------------------------------------------
# Pure detector functions
# --------------------------------------------------------------------------


def is_verbose(anchor: str, max_words: int = VERBOSE_THRESHOLD_WORDS) -> bool:
    """Anchor > N words is too long — signal-to-noise dilution + verbose
    style usually means doc-style or rambling LLM output."""
    if not anchor:
        return False
    return len(anchor.split()) > max_words


def find_doc_style_match(anchor: str) -> str:
    """Return first matched doc-style phrase, or empty string if clean."""
    if not anchor:
        return ""
    m = _DOC_STYLE_RE.search(anchor)
    return m.group(0) if m else ""


def is_capitalized_formal(anchor: str) -> bool:
    """First word starts uppercase + has Croatian imperative ending
    (-aj, -i, -uj, etc.) + anchor is not a question. Drivers type
    lowercase commands in WhatsApp."""
    if not anchor:
        return False
    if anchor.rstrip().endswith("?"):
        return False
    if not anchor[0].isupper():
        return False
    first_word = anchor.split()[0].rstrip(",.!:;").lower()
    if len(first_word) < 3:
        return False
    # Most-specific suffixes first (avoid "i" stealing "aj"-ending matches).
    return any(
        first_word.endswith(suf)
        for suf in sorted(_IMPERATIVE_SUFFIXES, key=len, reverse=True)
    )


def find_repetitive_verbs(
    anchors: list,
    threshold: int = REPETITIVE_VERB_THRESHOLD,
) -> list:
    """Return list of verb stems that appear at the start of `threshold`+
    anchors. Signal: LLM generated parafraza-svaki-anchor istog glagola.

    Example: ["Ažuriraj X", "Ažuriraj Y", "Ažuriraj Z"] → ["ažuri"]
    """
    if not anchors:
        return []
    stem_count: Counter = Counter()
    for anchor in anchors:
        if not isinstance(anchor, str) or not anchor:
            continue
        first_word = anchor.strip().split()[0] if anchor.strip() else ""
        first_word = first_word.rstrip(",.!?:;").lower()
        if len(first_word) >= _VERB_STEM_LEN:
            stem = first_word[:_VERB_STEM_LEN]
            stem_count[stem] += 1
    return sorted(
        stem for stem, count in stem_count.items() if count >= threshold
    )


def is_paraphrase_of_intent(
    anchor_vec, intent_vec,
    threshold: float = PARAPHRASE_COSINE_THRESHOLD,
) -> bool:
    """Cosine similarity test. None for either vector → returns False
    (we can't tell — caller falls back to other flags)."""
    if anchor_vec is None or intent_vec is None:
        return False
    return _cosine(anchor_vec, intent_vec) >= threshold


def _cosine(a, b) -> float:
    """Cosine similarity between two vectors. Returns 0.0 if either
    is zero-norm."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------
# Tool-level audit
# --------------------------------------------------------------------------


def audit_tool(
    tool_id: str,
    tool_data: dict,
    *,
    intent_vec=None,
    anchor_vecs=None,
) -> ToolAuditResult:
    """Run all 5 flag detectors against one tool's anchor list.

    intent_vec / anchor_vecs are optional: if None, paraphrase check is
    skipped (the other 4 flags still fire). Runner provides them after
    embedding the intent_summary via Azure.
    """
    anchors = tool_data.get("anchors") or []
    method = (tool_data.get("method") or "").upper()
    intent_summary = tool_data.get("intent_summary") or ""

    flags: list = []
    for i, anchor in enumerate(anchors):
        if not isinstance(anchor, str):
            continue

        if is_verbose(anchor):
            flags.append(AnchorFlag(
                anchor_index=i, anchor_text=anchor, flag=FLAG_VERBOSE,
                detail=f"{len(anchor.split())} riječi",
            ))

        doc_match = find_doc_style_match(anchor)
        if doc_match:
            flags.append(AnchorFlag(
                anchor_index=i, anchor_text=anchor, flag=FLAG_DOC_STYLE,
                detail=f"matched: '{doc_match}'",
            ))

        if is_capitalized_formal(anchor):
            flags.append(AnchorFlag(
                anchor_index=i, anchor_text=anchor,
                flag=FLAG_CAPITALIZED_FORMAL,
                detail="imperative form",
            ))

        # Paraphrase check requires embeddings
        if anchor_vecs is not None and intent_vec is not None:
            vec = anchor_vecs[i] if i < len(anchor_vecs) else None
            if vec is not None and is_paraphrase_of_intent(vec, intent_vec):
                flags.append(AnchorFlag(
                    anchor_index=i, anchor_text=anchor,
                    flag=FLAG_PARAPHRASE,
                    detail=f"cosine ≥ {PARAPHRASE_COSINE_THRESHOLD}",
                ))

    # Tool-level: repetitive verbs across all anchors
    tool_level: list = []
    rep_verbs = find_repetitive_verbs(anchors)
    for stem in rep_verbs:
        tool_level.append(f"{FLAG_REPETITIVE_VERBS}:{stem}")

    flagged_count = len({f.anchor_index for f in flags})

    return ToolAuditResult(
        tool_id=tool_id,
        method=method,
        intent_summary=intent_summary,
        anchor_count=len(anchors),
        flagged_anchors=flagged_count,
        flags=flags,
        tool_level_flags=tool_level,
    )


# --------------------------------------------------------------------------
# Aggregate stats + markdown report
# --------------------------------------------------------------------------


def aggregate_stats(results: list) -> dict:
    """Roll up per-tool results into report-summary numbers."""
    total_anchors = sum(r.anchor_count for r in results)
    flag_count: Counter = Counter()
    method_stats: dict = {}
    for r in results:
        for f in r.flags:
            flag_count[f.flag] += 1
        # tool-level flags counted as 1 per tool, not per anchor
        for tl in r.tool_level_flags:
            flag_count[tl.split(":")[0]] += 1
        m = r.method or "OTHER"
        ms = method_stats.setdefault(m, {"tools": 0, "anchors": 0, "flagged_anchors": 0})
        ms["tools"] += 1
        ms["anchors"] += r.anchor_count
        ms["flagged_anchors"] += r.flagged_anchors
    return {
        "total_tools": len(results),
        "total_anchors": total_anchors,
        "flag_counts": dict(flag_count),
        "method_stats": method_stats,
    }


def render_markdown_report(
    results: list,
    *,
    today: str,
    top_n_tools: int = 30,
) -> str:
    """Render Croatian markdown report. Sorted: worst tools first."""
    stats = aggregate_stats(results)
    lines: list = []

    lines.append(f"# Anchor quality audit — {today}")
    lines.append("")
    lines.append(
        f"Generated by `scripts/audit_anchor_quality.py` from "
        f"`config/tool_data.json`. "
        f"Total: **{stats['total_tools']} tools, {stats['total_anchors']} anchora**."
    )
    lines.append("")
    lines.append(
        "**Note**: flags su INDIKATORI, ne hard rules. Anchor s 1 flag-om "
        "može biti OK; tool s 6+ flagova vjerojatno treba re-curating."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Summary
    lines.append("## Summary po flagu")
    lines.append("")
    lines.append("| Flag | Count | % of anchora |")
    lines.append("|---|---|---|")
    total = max(1, stats["total_anchors"])
    for flag in (
        FLAG_VERBOSE, FLAG_PARAPHRASE, FLAG_DOC_STYLE,
        FLAG_CAPITALIZED_FORMAL, FLAG_REPETITIVE_VERBS,
    ):
        cnt = stats["flag_counts"].get(flag, 0)
        pct = 100.0 * cnt / total
        unit = "anchora" if flag != FLAG_REPETITIVE_VERBS else "tools"
        lines.append(f"| `{flag}` | {cnt} {unit} | {pct:.1f}% |")
    lines.append("")

    # Per-method breakdown
    lines.append("## Po HTTP method-u")
    lines.append("")
    lines.append("| Method | Tools | Anchora | Flagged | % flagged |")
    lines.append("|---|---|---|---|---|")
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OTHER"):
        ms = stats["method_stats"].get(method)
        if not ms:
            continue
        pct = 100.0 * ms["flagged_anchors"] / max(1, ms["anchors"])
        lines.append(
            f"| {method} | {ms['tools']} | {ms['anchors']} | "
            f"{ms['flagged_anchors']} | {pct:.1f}% |"
        )
    lines.append("")

    # Top-N worst tools
    sorted_results = sorted(
        results, key=lambda r: r.total_flag_count, reverse=True,
    )
    worst = [r for r in sorted_results if r.total_flag_count > 0][:top_n_tools]

    lines.append(f"## Top-{top_n_tools} tools po broj flag-ova")
    lines.append("")
    if not worst:
        lines.append("_Nema flagged tools — svi anchori su clean._")
    else:
        lines.append(
            "Tools s najviše problematičnih anchora. Filip ručno pregleda, "
            "popravlja u `config/tool_data.json`, briše "
            "`tests/benchmarks/router_anchor_cache.json` (force rebuild), "
            "restart bot."
        )
        lines.append("")
        for r in worst:
            lines.append(f"### `{r.tool_id}` ({r.flagged_anchors}/{r.anchor_count} flagged)")
            lines.append("")
            lines.append(f"- **Method**: {r.method}")
            lines.append(f"- **Intent**: {r.intent_summary[:120]}")
            if r.tool_level_flags:
                lines.append(
                    f"- **Tool-level flags**: {', '.join(r.tool_level_flags)}"
                )
            lines.append("")
            lines.append("| # | Anchor | Flags |")
            lines.append("|---|---|---|")
            # Group flags by anchor index for display
            flags_by_anchor: dict = {}
            for f in r.flags:
                flags_by_anchor.setdefault(f.anchor_index, []).append(f)
            for idx in sorted(flags_by_anchor.keys()):
                anchor_flags = flags_by_anchor[idx]
                anchor_text = anchor_flags[0].anchor_text[:80]
                flag_str = ", ".join(
                    f"`{f.flag}`" + (f" ({f.detail})" if f.detail else "")
                    for f in anchor_flags
                )
                lines.append(f"| {idx} | {anchor_text} | {flag_str} |")
            lines.append("")
            lines.append(
                "**Suggested action**: zamijeni flagged anchore s 5-6 "
                "user-style fraza tipa kako bi WhatsApp driver tipkao."
            )
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Anchor quality checklist (Filip 2026-05-17)")
    lines.append("")
    lines.append("Dobar anchor:")
    lines.append("- Krati i conversational (kako user tipka u WhatsApp-u)")
    lines.append("- 3+ smislenih riječi (premali anchori daju šum)")
    lines.append("- Multiple sinonimi za glagol (briši/ukloni/otkaži)")
    lines.append("- Specifičan entity (rezervacija, ne 'akcija')")
    lines.append("- Real user idiomi (kolokvijalno, ponekad bez diakritika)")
    lines.append("- Različit pristup od intent_summary, ne parafraza")
    lines.append("")
    lines.append(
        "Re-run nakon fix-eva za trend tracking. Cilj: smanjiti broj flag-ova "
        "kroz vrijeme dok ne ostanu samo legitimni edge cases."
    )

    return "\n".join(lines)
