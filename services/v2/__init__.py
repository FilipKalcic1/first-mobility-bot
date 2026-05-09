"""MobilityOne WhatsApp bot — v2 redesign.

Clean-slate rebuild after audit revealed 28k LOC bloat with 18% Top-1
on hardcore Croatian paraphrases. v2 targets ~6k LOC with deterministic
coverage of 70%+ driver traffic and statistical fallback for the rest.

Architecture (top-down):
  L0  Identity & Context — phone -> personId -> masterData (Redis 5min)
  L1  Special Intents    — welcome, GDPR, handover, help (hardcoded)
  L2  Driver Quick-Path  — ~14 patterns, deterministic, JSON config
  L3  Flow Detector      — booking, mileage, case (3 flows only)
  L4  Statistical Retr.  — FAISS+BM25+boost+rerank (FALLBACK)
  L5  Confidence Gate    — execute / fallback / ask-clarify decision
  L6  Mutation Safeguard — confirm dialog with concrete entity names
  L7  Tool Executor      — service routing + auth + tenant
  L8  Formatter + Sanity — Croatian template per tool family

See CLAUDE.md for engineering principles and self-rating discipline.
"""
