"""Active learning insights from production telemetry.

Reads `routing:accuracy_log:{tenant_id}` Redis lists (populated by
RedisSink in `services/v2/telemetry.py`) and computes 4 actionable
signals that Filip can use to improve anchors / thresholds:

  1. Per-tool negation rate (which tools confuse users)
  2. Repeated queries with unstable tool picks (router is inconsistent)
  3. Confidence-bucket negation distribution (threshold tuning hint)
  4. Tool coverage (picked vs never-picked tools)

CRITICAL: TelemetryEvent does NOT carry `phone` / `phone_hash` (privacy
decision — see engine.py:_log_telemetry "phone_hash" in legacy drop list).
So we cannot precisely link Turn N (tool A picked) → Turn N+1 ("nije
točno") → Turn N+2 (tool B picked) for the same end-user.

What we CAN do: within a tenant, treat consecutive events as likely
related (small noise from cross-user mixing in busy tenants — acceptable
for "which tools are problematic" signal, not for ground-truth pairing).

For TRUE correction-pair extraction (FAZA 6), a privacy decision is
needed to add a session marker.

All functions in this module are pure (no I/O). The `scripts/run_active_learning.py`
runner does the Redis read.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass


# Confidence histogram buckets — boundaries match what confidence_gate
# considers HIGH (>= 0.7) / MEDIUM (>= 0.5) / LOW (anything below).
_CONFIDENCE_BUCKETS: tuple = (
    (0.0, 0.3, "0.0-0.3"),
    (0.3, 0.5, "0.3-0.5"),
    (0.5, 0.7, "0.5-0.7"),
    (0.7, 0.9, "0.7-0.9"),
    (0.9, 1.01, "0.9-1.0"),  # 1.01 to include exact 1.0
)


@dataclass(frozen=True)
class NegationRateRow:
    tool_id: str
    picks: int
    negation_count: int

    @property
    def rate(self) -> float:
        if self.picks == 0:
            return 0.0
        return self.negation_count / self.picks


# --------------------------------------------------------------------------
# Pure analysis functions
# --------------------------------------------------------------------------


def extract_negation_per_tool(events: list) -> list:
    """For each picked tool, compute (picks, negation_count) where
    negation_count = number of times the NEXT event from the same tenant
    had `is_negation=True`.

    Imperfect — events are tenant-scoped not session-scoped, so a different
    user's turn could be counted as "next". For high-volume tenants this
    adds noise; for low-volume tenants (single driver per tenant) the
    signal is essentially per-user.

    Returns list of `NegationRateRow` sorted by (rate desc, picks desc).
    """
    by_tenant: dict = defaultdict(list)
    for e in events:
        by_tenant[e.get("tenant_id") or ""].append(e)

    picks_per_tool: Counter = Counter()
    neg_per_tool: Counter = Counter()
    for evs in by_tenant.values():
        # Sort by ts (chronological order). Redis LPUSH stores newest
        # first; caller may pass in either order — normalize here.
        evs.sort(key=lambda e: e.get("ts") or "")
        for i, e in enumerate(evs):
            tool = e.get("tool_picked")
            if not tool:
                continue
            picks_per_tool[tool] += 1
            # Look at next event in same tenant
            if i + 1 < len(evs) and evs[i + 1].get("is_negation"):
                neg_per_tool[tool] += 1

    rows = [
        NegationRateRow(
            tool_id=tool, picks=picks,
            negation_count=neg_per_tool.get(tool, 0),
        )
        for tool, picks in picks_per_tool.items()
    ]
    # Sort: highest rate first, ties broken by picks (more data = more reliable)
    rows.sort(key=lambda r: (-r.rate, -r.picks))
    return rows


def extract_query_patterns(
    events: list, min_repeats: int = 3, min_distinct_tools: int = 2,
) -> list:
    """Queries (scrubbed text) seen many times where the router picked
    different tools across occurrences — signal that anchors are
    ambiguous for that phrasing.

    Returns list of dicts: {query, total_occurrences, tool_distribution}
    sorted by total_occurrences desc. Empty list if no patterns match.
    """
    by_query: dict = defaultdict(lambda: Counter())
    for e in events:
        q = (e.get("query_scrubbed") or "").strip()
        if not q:
            continue
        tool = e.get("tool_picked")
        if not tool:
            continue
        by_query[q][tool] += 1

    out = []
    for q, tool_counts in by_query.items():
        total = sum(tool_counts.values())
        if total < min_repeats:
            continue
        if len(tool_counts) < min_distinct_tools:
            continue
        out.append({
            "query": q,
            "total_occurrences": total,
            "distinct_tools": len(tool_counts),
            "tool_distribution": dict(tool_counts.most_common()),
        })
    out.sort(key=lambda r: -r["total_occurrences"])
    return out


def extract_confidence_distribution(events: list) -> list:
    """Bucket router confidence into 5 ranges. Within each bucket count
    picks + negations (next-turn) → negation rate.

    If bucket [0.5, 0.7) has a much higher negation rate than the
    confidence_gate's threshold assumes, the threshold should be
    revisited.
    """
    by_tenant: dict = defaultdict(list)
    for e in events:
        by_tenant[e.get("tenant_id") or ""].append(e)

    bucket_picks: Counter = Counter()
    bucket_neg: Counter = Counter()
    for evs in by_tenant.values():
        evs.sort(key=lambda e: e.get("ts") or "")
        for i, e in enumerate(evs):
            conf = e.get("confidence")
            if conf is None:
                continue
            if not e.get("tool_picked"):
                continue
            label = _confidence_bucket(float(conf))
            bucket_picks[label] += 1
            if i + 1 < len(evs) and evs[i + 1].get("is_negation"):
                bucket_neg[label] += 1

    out = []
    for _, _, label in _CONFIDENCE_BUCKETS:
        picks = bucket_picks.get(label, 0)
        negs = bucket_neg.get(label, 0)
        out.append({
            "bucket": label,
            "picks": picks,
            "negation_count": negs,
            "negation_rate": (negs / picks) if picks else 0.0,
        })
    return out


def extract_tool_coverage(events: list, all_tool_ids: set) -> dict:
    """Identify never-picked tools. These either need stronger anchors
    or should be removed from scope (dead weight slowing the router)."""
    picked: set = set()
    for e in events:
        tool = e.get("tool_picked")
        if tool and tool in all_tool_ids:
            picked.add(tool)
    unpicked = all_tool_ids - picked
    total = len(all_tool_ids) or 1
    return {
        "picked_count": len(picked),
        "unpicked_count": len(unpicked),
        "coverage_pct": 100.0 * len(picked) / total,
        "picked": sorted(picked),
        "unpicked": sorted(unpicked),
    }


def _confidence_bucket(conf: float) -> str:
    for low, high, label in _CONFIDENCE_BUCKETS:
        if low <= conf < high:
            return label
    # Out of range (negative or >1.0) — bucket into edges
    if conf < 0.0:
        return _CONFIDENCE_BUCKETS[0][2]
    return _CONFIDENCE_BUCKETS[-1][2]


# --------------------------------------------------------------------------
# Markdown report renderer
# --------------------------------------------------------------------------


def render_markdown_report(
    *,
    total_events: int,
    today: str,
    negation_rates: list,
    query_patterns: list,
    confidence_dist: list,
    tool_coverage: dict = None,
    top_n: int = 20,
) -> str:
    """Render a Croatian markdown report. All inputs are outputs from
    the extract_* functions above. Returns a single string ready to
    write to disk or stdout."""
    lines: list = [
        f"# Active learning report — {today}",
        "",
        f"Generated from `routing:accuracy_log:*` Redis lists. "
        f"Total events analyzed: **{total_events}**.",
        "",
        "**LIMITACIJA**: telemetry NEMA `phone_hash` (privacy decision). "
        "Cross-turn 'next event' aproksimacija je tenant-scoped, ne "
        "session-scoped. Za high-volume tenante može imati šum od cross-user "
        "miksanja — koristi kao signal, ne kao ground truth.",
        "",
        "---",
        "",
    ]

    # Section 1: Negation rate per tool
    lines.append("## 1. Top tools po negation rate (sljedeći turn 'nije točno')")
    lines.append("")
    if not negation_rates:
        lines.append("_Nema podataka — bot nije picked nijedan tool u dosadašnjim turnovima._")
    else:
        lines.append("| Rank | Tool ID | Picks | Negations | Rate |")
        lines.append("|---|---|---|---|---|")
        # Filter: only tools with ≥3 picks (otherwise rate is noisy)
        meaningful = [r for r in negation_rates if r.picks >= 3]
        if not meaningful:
            lines.append("| _- | (nedovoljno podataka per tool, prag = 3 picks) | - | - | - |")
        for i, row in enumerate(meaningful[:top_n], start=1):
            lines.append(
                f"| {i} | `{row.tool_id}` | {row.picks} | "
                f"{row.negation_count} | {row.rate:.1%} |"
            )
        lines.append("")
        lines.append(
            "**Action**: tools s rate > 30% imaju problem s anchorima ili "
            "intent_summary nije jasan. Otvori taj tool u `config/tool_data.json`, "
            "pregledaj anchore, dodaj jasnije/uže Croatian fraze."
        )
    lines.append("")

    # Section 2: Unstable queries
    lines.append("## 2. Top queries gdje router griješi (isti query → različiti tools)")
    lines.append("")
    if not query_patterns:
        lines.append("_Nema query-ja s ≥3 ponavljanja i ≥2 različita tool-a._")
    else:
        lines.append("| Rank | Query | Occurrences | Distinct tools | Distribution |")
        lines.append("|---|---|---|---|---|")
        for i, qp in enumerate(query_patterns[:top_n], start=1):
            dist = ", ".join(
                f"`{t}`×{c}" for t, c in qp["tool_distribution"].items()
            )[:200]
            lines.append(
                f"| {i} | `{qp['query'][:60]}` | {qp['total_occurrences']} | "
                f"{qp['distinct_tools']} | {dist} |"
            )
        lines.append("")
        lines.append(
            "**Action**: za svaki redak, odluči koji je 'pravi' tool, "
            "dodaj query string kao novi anchor tom tool-u (i možda makni "
            "konfliktnu frazu iz drugih tools-a)."
        )
    lines.append("")

    # Section 3: Confidence distribution
    lines.append("## 3. Confidence distribution + negation rate per bucket")
    lines.append("")
    lines.append("| Bucket | Picks | Negation count | Negation rate |")
    lines.append("|---|---|---|---|")
    for row in confidence_dist:
        lines.append(
            f"| {row['bucket']} | {row['picks']} | "
            f"{row['negation_count']} | {row['negation_rate']:.1%} |"
        )
    lines.append("")
    lines.append(
        "**Action**: ako 0.5-0.7 bucket ima visok negation rate (npr > 20%), "
        "podigni `MIN_CONF_MEDIUM` u `services/v2/confidence_gate.py`. "
        "Ako 0.7-0.9 ima nizak rate (npr < 5%), threshold možda može pasti."
    )
    lines.append("")

    # Section 4: Tool coverage
    if tool_coverage is not None:
        lines.append("## 4. Tool coverage (picked vs never-picked)")
        lines.append("")
        lines.append(
            f"- Tools picked at least once: **{tool_coverage['picked_count']}** "
            f"({tool_coverage['coverage_pct']:.1f}%)"
        )
        lines.append(
            f"- Tools NEVER picked: **{tool_coverage['unpicked_count']}**"
        )
        lines.append("")
        unpicked = tool_coverage.get("unpicked") or []
        if unpicked:
            lines.append(
                f"### Sample never-picked tools (prvih {min(50, len(unpicked))} od {len(unpicked)})"
            )
            lines.append("")
            for tool in unpicked[:50]:
                lines.append(f"- `{tool}`")
            if len(unpicked) > 50:
                lines.append(f"- _(+{len(unpicked) - 50} more)_")
            lines.append("")
            lines.append(
                "**Action**: provjeri jesu li ovo tools koji su iz Filip "
                "scope-a (driver use case)? Ako nisu — ostavi, manager-i će "
                "ih pozvati. Ako jesu — anchori su preslabi za matching."
            )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Generated by `scripts/run_active_learning.py`. Re-run periodically "
        "to track trend over time."
    )
    return "\n".join(lines)
