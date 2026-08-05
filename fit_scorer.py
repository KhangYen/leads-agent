#!/usr/bin/env python3
"""fit_scorer.py — programmatic fit scorer for LeadsAgent.

Maps a Reddit task post to the operator's capability_profile.json and returns
a deterministic FitResult. Mirrors the dataclass+score shape of
demand_signals/analysis/matcher.py but is profile-driven (reads
capability_profile.json) and covers all guardrails buckets, not just DE.

Why a code scorer (vs LLM-judged prose):
  - Deterministic: same post + profile = same score, every run.
  - Fast + free: no model call. The scan loop can score 100 posts/sec.
  - Saleable: this is the "secret sauce" a Fiverr client is paying for -
    their profile -> ranked leads, reproducibly.

Usage:
  from fit_scorer import score_post, FitResult
  result = score_post(post_dict, profile)   # post_dict has title, selftext, payout_usd, rail, subreddit

  # CLI (for unit tests / debugging):
  python fit_scorer.py --json < post.json
"""
from __future__ import annotations
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = THIS_DIR / "capability_profile.json"


@dataclass
class FitResult:
    """Score one post against the capability profile."""
    post_id: str
    title: str
    top_bucket: str            # e.g. "data" / "content" / "code" / "" if no fit
    bucket_scores: dict        # {bucket: 0.0-1.0 normalized keyword hit}
    matched_keywords: list     # keywords that fired
    anti_keywords_hit: list    # ToS-adjacent / reject signals hit (-> hard skip)
    rail_ok: bool              # payment rail acceptable for SEA
    payout_ok: bool            # meets the floor for the subreddit
    fit_score_1_5: int         # final 1-5 fit score (0 = reject)
    reject_reason: str         # "" if accepted, else why we skipped
    opportunity_0_100: float   # composite (fit × payout × recency) for ranking


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + normalize curly quotes to ASCII.

    Curly apostrophe (U+2019) -> straight ' so that "I'll" matches the straight-
    apostrophe keyword lists. Real Reddit text uses curly quotes; our signal
    lists use ASCII. Without this, "I'll build" (curly) never matches
    "i'll build" (straight) and mis-tagged sellers leak through.
    """
    s = (text or "").lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'")  # curly single quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"')  # curly double quotes
    return re.sub(r"\s+", " ", s).strip()


# Seller-prefix detection (case-insensitive). Posts starting with these are
# OTHER SELLERS offering services, not buyers posting tasks. Hard-skip them.
SELLER_PREFIXES = ("[offer]", "[service]", "[for hire]", "[forhire]", "offer:", "service:")

# Buyer-post signals (case-insensitive substring). A post is a candidate buyer
# if its title starts with one of these OR its body contains a $ amount.
BUYER_PREFIXES = ("[task]", "[hiring]", "[paid]", "[request]", "task:", "hiring:")


# Body-text seller signals: phrases that, if present in the body, strongly
# indicate the post is an OFFER (a seller advertising services), even if the
# title uses a [TASK]-style tag. r/slavelabour has sellers who mis-tag.
SELLER_BODY_SIGNALS = (
    "i will build", "i'll build", "i will create", "i'll create",
    "i will design", "i'll design", "i will write", "i'll write",
    "i can help you with", "my services include", "i offer",
    "hire me", "dm me to discuss your project", "check out my portfolio",
    "i'm a ", "i am a ",  # "I'm a developer", "I am a designer" = seller intro
)


def classify_post(title: str, selftext: str = "") -> str:
    """Return 'seller', 'buyer', or 'unknown' based on title/body signals.

    This is the shared filter primitive the SKILL.md scan loop and any wrapper
    should call before scoring. Catches:
      - mixed-case [OFFER]/[Offer]/[offer] title variants
      - sellers who mis-tag with [TASK] but reveal themselves in the body
        (e.g. "[task] I'll build you a website for $50" is a seller ad)
    """
    t = _normalize(title)
    if t.startswith(SELLER_PREFIXES):
        return "seller"
    # "for hire" anywhere in title = seller (r/forhire [For Hire] tag variant)
    if "for hire" in t or "will build" in t or "i will " in t[:20]:
        return "seller"
    # Body OR title seller signals: even with a [TASK] tag, these phrases = seller.
    # Checks title too because sellers mis-tag in the title itself
    # (e.g. "[task] I'll build you a website for $50" is a seller ad).
    b = _normalize(selftext)
    if any(sig in b[:300] for sig in SELLER_BODY_SIGNALS) or any(sig in t for sig in SELLER_BODY_SIGNALS):
        return "seller"
    if t.startswith(BUYER_PREFIXES):
        return "buyer"
    # fallback: $ amount in body = likely a buyer task
    if re.search(r"\$\d+", selftext or ""):
        return "buyer"
    return "unknown"



def _keyword_hits(text_norm: str, keywords: list) -> list:
    """Return the subset of keywords present in text. Phrase-aware.

    SHORT-KEYWORD GUARD: tokens <=3 chars (ts, api, etl, csv, sql) are matched
    on word boundaries only, not as substrings. Without this, 'ts' matches
    'transgender'/'timestamp'/'its'/'wants' and every post inflates to a code
    fit. Multi-word phrases (>=4 chars or containing a space) use substring
    match (correct for 'python', 'dbt', 'google apps script').
    """
    hits = []
    for kw in keywords:
        if len(kw) <= 3 and " " not in kw:
            # word-boundary match for short tokens
            if re.search(rf"\b{re.escape(kw)}\b", text_norm):
                hits.append(kw)
        else:
            if kw in text_norm:
                hits.append(kw)
    return hits


def _is_tos_skip(text_norm: str, tos_skips: list) -> list:
    """Return any ToS-skip phrases that fire. Non-empty = hard reject."""
    return [t for t in tos_skips if t in text_norm]


def _check_rail(text_norm: str, profile_rails: dict) -> tuple[bool, str]:
    """True if the post's stated rail is SEA-acceptable, or unstated (negotiable)."""
    accepted = [r.lower() for r in profile_rails.get("accepted", [])]
    rejected = [r.lower() for r in profile_rails.get("rejected", [])]
    # Look for any rejected rail explicitly mentioned
    for r in rejected:
        if r in text_norm:
            return False, f"rejected rail '{r}' mentioned"
    # If no rail mentioned at all, leave it open (negotiable in pitch)
    # If an accepted rail is mentioned, great; otherwise neutral.
    return True, ""


def _check_payout(payout_usd: float | int | None, subreddit: str) -> tuple[bool, str]:
    """Floor: $5 for slavelabour, $20 for forhire. None/0 = unstated (ok, flag)."""
    if payout_usd is None or payout_usd == 0:
        return True, "payout unstated"
    floor = 5 if "slavelabour" in (subreddit or "").lower() else 20
    if payout_usd < floor:
        return False, f"${payout_usd} below ${floor} floor for r/{subreddit}"
    return True, ""


def score_post(post: dict, profile: dict | None = None, profile_path: Path = DEFAULT_PROFILE) -> FitResult:
    """Score a single post. post has: id, title, selftext, payout_usd, rail, subreddit, created_utc."""
    if profile is None:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))

    title = post.get("title", "")
    body = post.get("selftext", "")
    full = _normalize(f"{title}\n{body}")

    # --- Hard rejects first (ToS / rail / payout) ---
    anti = _is_tos_skip(full, profile.get("tos_skips", []))
    if anti:
        return FitResult(
            post_id=post.get("id", ""), title=title, top_bucket="",
            bucket_scores={}, matched_keywords=[], anti_keywords_hit=anti,
            rail_ok=False, payout_ok=False, fit_score_1_5=0,
            reject_reason=f"ToS-skip: {', '.join(anti)}",
            opportunity_0_100=0.0,
        )

    rail_ok, rail_msg = _check_rail(full, profile.get("rails", {}))
    payout_ok, payout_msg = _check_payout(post.get("payout_usd"), post.get("subreddit", ""))
    if not rail_ok:
        return FitResult(post_id=post.get("id",""), title=title, top_bucket="",
            bucket_scores={}, matched_keywords=[], anti_keywords_hit=[],
            rail_ok=False, payout_ok=payout_ok, fit_score_1_5=0,
            reject_reason=rail_msg, opportunity_0_100=0.0)
    if not payout_ok:
        return FitResult(post_id=post.get("id",""), title=title, top_bucket="",
            bucket_scores={}, matched_keywords=[], anti_keywords_hit=[],
            rail_ok=rail_ok, payout_ok=False, fit_score_1_5=0,
            reject_reason=payout_msg, opportunity_0_100=0.0)

    # --- Per-bucket keyword scoring ---
    caps = profile.get("capabilities", {})
    bucket_scores: dict[str, float] = {}
    matched: list[str] = []
    for bucket, spec in caps.items():
        kws = spec.get("keywords", [])
        if not kws:
            continue
        hits = _keyword_hits(full, kws)
        # normalize: 1 hit = 0.4, 2 = 0.7, 3+ = 1.0 (diminishing returns)
        if hits:
            bucket_scores[bucket] = min(1.0, 0.4 + 0.3 * (len(hits) - 1))
            matched.extend(hits)
        else:
            bucket_scores[bucket] = 0.0

    # --- Fit score 1-5 ---
    if not bucket_scores or max(bucket_scores.values()) == 0.0:
        # No keyword match at all - not a fit. Don't reject (could be a
        # generic task with unstated needs) but score 1 = weak.
        return FitResult(post_id=post.get("id",""), title=title, top_bucket="",
            bucket_scores=bucket_scores, matched_keywords=matched,
            anti_keywords_hit=[], rail_ok=rail_ok, payout_ok=payout_ok,
            fit_score_1_5=1, reject_reason="",
            opportunity_0_100=_opportunity(1, post.get("payout_usd", 0), post.get("created_utc", 0)))

    top_bucket = max(bucket_scores, key=bucket_scores.get)
    top_score = bucket_scores[top_bucket]
    strength = caps[top_bucket].get("strength", 3)
    # fit_score_1_5: combine keyword-density (top_score) with the operator's
    # self-rated strength in that bucket. Strong bucket + high density = 5.
    raw = top_score * (strength / 5.0)
    fit_1_5 = max(1, min(5, round(1 + raw * 4)))  # 1..5

    return FitResult(
        post_id=post.get("id", ""), title=title, top_bucket=top_bucket,
        bucket_scores=bucket_scores, matched_keywords=matched,
        anti_keywords_hit=[], rail_ok=rail_ok, payout_ok=payout_ok,
        fit_score_1_5=fit_1_5, reject_reason="",
        opportunity_0_100=_opportunity(fit_1_5, post.get("payout_usd", 0), post.get("created_utc", 0)),
    )


def _opportunity(fit_1_5: int, payout_usd: float, created_utc: float, now: float | None = None) -> float:
    """Composite rank score 0-100. fit dominates; payout scales; recency decays.

    Mirrors matcher.py opportunity_score's combine pattern:
      skill_fit * 30 + money * 30 + recency * (engagement weight, omitted here).
    """
    import time
    if now is None:
        now = time.time()
    fit_norm = fit_1_5 / 5.0
    # money: log-ish, capped. $50 -> ~0.7, $200 -> ~0.9, $1000 -> ~1.0
    p = float(payout_usd or 0)
    money_norm = min(1.0, (p / 50.0) ** 0.5) if p > 0 else 0.3
    # recency: 1.0 if <6h, 0.7 if <24h, 0.4 otherwise
    age_h = (now - (created_utc or now)) / 3600.0
    recency = 1.0 if age_h < 6 else (0.7 if age_h < 24 else 0.4)
    return round((fit_norm * 40 + money_norm * 30 + recency * 30), 1)


# --- CLI for unit tests / debugging ---
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Score a Reddit post against the capability profile.")
    ap.add_argument("--json", action="store_true", help="Output FitResult as JSON")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    args = ap.parse_args()
    post = json.loads(sys.stdin.read())
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    result = score_post(post, profile)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"post:    {result.title[:70]}")
        print(f"fit:     {result.fit_score_1_5}/5  (bucket: {result.top_bucket or '-'})")
        print(f"opp:     {result.opportunity_0_100}/100")
        if result.reject_reason:
            print(f"REJECT:  {result.reject_reason}")
        if result.matched_keywords:
            print(f"matched: {', '.join(result.matched_keywords[:8])}")
