#!/usr/bin/env python3
"""dm_drafter.py — auto-draft a Reddit pitch DM from a scored post.

Architecture: Python does the deterministic piece-selection (which hook,
which portfolio line, which honesty disclosure based on top_bucket); the
SKILL.md (Claude) renders the final prose. This file produces:
  1. A complete baseline DM (paste-able, template prose) in the existing
     state/reddit_submission_draft_<taskid>.md format.
  2. A "refine" prompt Claude can run to upgrade the baseline into natural
     prose before the human reviews it.

The baseline respects the tone_rules: ≤150 words, honest disclosure before
CTA, real portfolio links, named price only if OP stated a budget.

Usage:
  from dm_drafter import draft_dm
  draft = draft_dm(post, fit_result, capability_profile, pitch_profile, subreddit)
  # draft has: .filepath, .baseline_dm, .refine_prompt, .word_count
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_CAPS = THIS_DIR / "capability_profile.json"
DEFAULT_PITCH = THIS_DIR / "pitch_profile.json"
STATE_DIR = THIS_DIR.parents[2] / "state"   # .../Passive incomes/1/state

# Disclosure picker: which honesty_gaps entry fires for which bucket/keyword
def _pick_disclosure(text_norm: str, top_bucket: str, caps_profile: dict, pitch_profile: dict) -> str:
    """Return the single most-relevant honesty disclosure for this post."""
    disclosures = pitch_profile.get("honesty_disclosures", {})
    # audience: writing tasks that imply the OP wants readers
    if top_bucket == "content" and re.search(r"substack|medium|audience|subscriber|reader|blog", text_norm):
        return disclosures.get("audience", "")
    # karma: tasks that imply the doer needs Reddit presence (e.g., "must have X karma", "post on my behalf")
    if re.search(r"karma|reddit account|post on|comment on your behalf|aged account", text_norm):
        return disclosures.get("karma", "")
    # design scope: visual tasks
    if top_bucket == "visuals":
        return disclosures.get("design_scope", "")
    # payment rail: if the OP didn't state a rail, flag SEA early
    # (detect: no rail keyword anywhere in the post)
    rail_re = re.compile(r"paypal|wise|payoneer|usdt|crypto|revolut|venmo|zelle|ach|bank|wire")
    if not rail_re.search(text_norm):
        return disclosures.get("payment_rail", "")
    # default: no disclosure needed
    return ""


def _pick_portfolio(top_bucket: str, pitch_profile: dict) -> str:
    return pitch_profile.get("portfolio_selection", {}).get(top_bucket,
           pitch_profile.get("portfolio_selection", {}).get("code", ""))


def _pick_cta(top_bucket: str, pitch_profile: dict) -> str:
    ctas = pitch_profile.get("cta_templates", {})
    return ctas.get(top_bucket, ctas.get("default", ""))


@dataclass
class DraftResult:
    filepath: Path
    baseline_dm: str
    refine_prompt: str
    word_count: int
    over_limit: bool


def draft_dm(
    post: dict,
    fit_result,                      # FitResult from fit_scorer
    caps_profile: dict | None = None,
    pitch_profile: dict | None = None,
    caps_path: Path = DEFAULT_CAPS,
    pitch_path: Path = DEFAULT_PITCH,
    state_dir: Path = STATE_DIR,
) -> DraftResult:
    """Build the DM draft file. Returns the filepath + the baseline DM text."""
    if caps_profile is None:
        caps_profile = json.loads(caps_path.read_text(encoding="utf-8"))
    if pitch_profile is None:
        pitch_profile = json.loads(pitch_path.read_text(encoding="utf-8"))

    subreddit = post.get("subreddit", "slavelabour").replace("r/", "")
    title = post.get("title", "")
    body = post.get("selftext", "")
    payout = post.get("payout_usd")
    url = post.get("url", "")
    task_id = post.get("id", "unknown")
    op = post.get("author", "")
    top_bucket = fit_result.top_bucket or "code"
    text_norm = re.sub(r"\s+", " ", f"{title}\n{body}".lower())

    # --- Assemble the baseline DM body (template prose) ---
    opener = pitch_profile.get("opener", "Hi - saw your task on r/{subreddit}.").format(subreddit=subreddit)
    niche = pitch_profile.get("niche_statement", "")
    bucket_pitch = caps_profile.get("capabilities", {}).get(top_bucket, {}).get("pitch_angle", "")
    portfolio = _pick_portfolio(top_bucket, pitch_profile)
    disclosure = _pick_disclosure(text_norm, top_bucket, caps_profile, pitch_profile)
    cta = _pick_cta(top_bucket, pitch_profile)

    # price line: only if OP stated a budget
    price_line = ""
    if payout and payout > 0:
        price_line = f"Your stated rate (${payout}) works for me."

    dm_lines = [opener]
    if niche:
        dm_lines.append(niche)
    if bucket_pitch:
        dm_lines.append(bucket_pitch)
    if portfolio:
        dm_lines.append(f"Samples: {portfolio}")
    if disclosure:
        dm_lines.append(disclosure)
    if price_line:
        dm_lines.append(price_line)
    if cta:
        dm_lines.append(cta)

    baseline_dm = " ".join(dm_lines)
    # collapse whitespace
    baseline_dm = re.sub(r"\s+", " ", baseline_dm).strip()
    word_count = len(baseline_dm.split())
    over_limit = word_count > 150

    # --- Build the refine prompt (for Claude to upgrade into natural prose) ---
    bucket_kw = ", ".join(fit_result.matched_keywords[:6]) if fit_result.matched_keywords else "(none)"
    refine_prompt = (
        f"Rewrite this Reddit pitch DM into natural, human prose under 150 words.\n"
        f"Keep every fact. Fix the flow. One voice - a busy freelancer texting, not a template.\n\n"
        f"TASK (r/{subreddit}, ${payout or 'unstated'}): {title}\n"
        f"POST EXCERPT: {body[:300]}\n\n"
        f"BUCKET FIT: {top_bucket} (matched: {bucket_kw})\n"
        f"BASELINE DRAFT ({word_count} words):\n---\n{baseline_dm}\n---\n\n"
        f"HARD RULES: ≤150 words. No emoji walls. No hype. Disclose the honesty gap BEFORE the CTA. "
        f"Real portfolio links only. Name a price only if OP stated one."
    )

    # --- Write the draft file in the existing format ---
    state_dir.mkdir(parents=True, exist_ok=True)
    filepath = state_dir / f"reddit_submission_draft_{task_id}.md"

    # the "what you're agreeing to" + risk note are bucket-aware
    obligations, risk_note = _obligations_and_risk(top_bucket, payout, post, caps_profile)

    file_content = f"""# Reddit DM draft — for user review (rule 8 gate)

**To:** u/{op or "(OP handle)"}
**Thread:** {url}
**Payout:** ${payout or "(unstated)"} {post.get('rail', '')}
**Task:** {title[:140]}
**Fit:** {fit_result.fit_score_1_5}/5 (bucket: {top_bucket}) · opp {fit_result.opportunity_0_100}/100 · matched: {bucket_kw}

---

## DM text (paste into Reddit chat/DM — under 150 words)

{baseline_dm}

**Word count: {word_count}** {"⚠ OVER 150 — trim before sending" if over_limit else "✓ under limit"}

---

## Refine prompt (optional — paste to Claude for a better-prose version)

```
{refine_prompt}
```

---

## What you're agreeing to if you send this
{obligations}

## Honest risk note
{risk_note}
"""
    filepath.write_text(file_content, encoding="utf-8")

    return DraftResult(
        filepath=filepath, baseline_dm=baseline_dm,
        refine_prompt=refine_prompt, word_count=word_count, over_limit=over_limit,
    )


def _obligations_and_risk(top_bucket: str, payout, post: dict, caps_profile: dict) -> tuple[str, str]:
    """Bucket-aware 'what you're agreeing to' + risk note. Mirrors the existing format."""
    rail = post.get("rail", "")
    obligations = [
        f"- You deliver the work described; OP pays ${payout or 'the agreed amount'} {('via ' + rail) if rail else 'via the stated rail'}.",
        "- **You send the DM and any follow-up in your own browser.** The agent never posts to Reddit (rule 8 + 1-karma account safety).",
        "- If the OP responds, you handle the delivery + payment directly.",
    ]
    risks = []
    if top_bucket == "content":
        risks.append("- **Audience mismatch is real** for writing tasks gating on existing readers. The DM discloses this; the OP may decline. If so, log FAILED and move on.")
    if not payout:
        risks.append("- **Payout unstated** — agree a price in DM before doing any work. Never deliver before the price is locked.")
    if rail and rail.lower() in ("zelle", "ach", "wire", "bank"):
        risks.append(f"- **Rail '{rail}' may not be SEA-receivable.** Confirm PayPal/Wise/USDT before committing.")
    risks.append("- **On-publication / on-delivery payment** = you may do work before paying. Standard for these gigs; never hand over the final until the OP has confirmed acceptance (protects against ghosting).")
    risks.append("- **PayPal/Wise receivable in Vietnam:** yes.")
    return "\n".join(obligations), "\n".join(risks)


# --- CLI for unit tests / debugging ---
if __name__ == "__main__":
    import sys, time
    sys.path.insert(0, str(THIS_DIR))
    from fit_scorer import score_post
    post = json.loads(sys.stdin.read())
    caps = json.loads(DEFAULT_CAPS.read_text(encoding="utf-8"))
    pitch = json.loads(DEFAULT_PITCH.read_text(encoding="utf-8"))
    fit = score_post(post, caps)
    if fit.reject_reason or fit.fit_score_1_5 == 0:
        print(f"SKIP (rejected): {fit.reject_reason or 'fit 0'}", file=sys.stderr)
        sys.exit(1)
    d = draft_dm(post, fit, caps, pitch)
    print(f"wrote: {d.filepath}")
    print(f"words: {d.word_count} {'(OVER)' if d.over_limit else ''}")
    print(f"\n--- baseline DM ---\n{d.baseline_dm}")
