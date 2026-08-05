# LeadsAgent

> An AI agent that scans Reddit for your ideal clients and pre-drafts your pitch DMs. Built on a real pipeline that runs in production. No hype, no course - the working code.

## What it does

Give LeadsAgent your **capability profile** (what you can deliver, your portfolio, your payment rails) and the subreddits where your clients post (r/slavelabour, r/forhire, r/DoneDirtCheap, niche subs). Every run it:

1. **Fetches** the newest posted tasks via the opencli Reddit adapter.
2. **Filters out sellers** (the `[Offer]` / `[For Hire]` posts that clutter these subs - case-insensitive, catches all variants).
3. **Scores each buyer task** against your profile - programmatically, not vibes. A code task scores high for a developer; a writing task scores high for a writer. ToS-adjacent tasks (fake reviews, mass account creation) and unreceivable payment rails (ACH/Zelle for non-US) are hard-rejected.
4. **Ranks** the survivors by opportunity score (fit × payout × recency).
5. **Drafts a pitch DM** for the top N - under 150 words, honest about your gaps, with your real portfolio links, ready to paste.

You review the drafts and paste them in your own browser. **LeadsAgent never sends anything** - that's your call, your account, your reputation.

## Why this exists

The r/aiagents community (the people actually doing this work, not selling courses) was asked: *"Are people actually making serious money selling AI automations in 2026, or is it mostly course marketing?"* The top answer was *"In the gold rush, the best business is to sell the shovels."* LeadsAgent is a shovel - a real one, with working code, not a $497 PDF about shovels.

It was built to **dogfood first**: the operator runs it on their own r/slavelabour pipeline (data-engineering / dbt / SQL / Excel work). The same code, with a different `capability_profile.json`, works for any freelancer - designers, writers, translators, VAs, niche consultants.

## The 5-minute setup

```bash
# 1. Requirements: Python 3.10+ and the opencli Reddit adapter
#    (npm install -g @opencli/reddit, then log into Reddit in your browser;
#    the adapter reuses that session).

# 2. Clone and copy the profile template:
git clone https://github.com/KhangYen/leads-agent.git
cd leads-agent
cp capability_profile.example.json capability_profile.json
# Edit capability_profile.json with YOUR skills, portfolio, rails, gaps

# 3. Run a scan:
python run_scan.py --top 3
#   --subreddits slavelabour,forhire   (default)
#   --profile capability_profile.json  (default)
#   --no-draft                         (score + rank only)

# 4. Review state/reddit_tasks_dryrun.md (ranked table) and the drafted DMs
#    in state/reddit_submission_draft_*.md. Paste the ones you want in your
#    own browser. LeadsAgent never posts.
```

## What you get per run

- `state/reddit_tasks_dryrun.md` - ranked markdown table of fitting tasks.
- `state/reddit_submission_draft_<id>.md` - one DM draft per top task, in paste-ready format with: the DM text (≤150 words), a refine-prompt you can hand to Claude for a prose upgrade, and an honest "what you're agreeing to + risk note" for your review.

## The honesty rules (encoded in the code)

- **Real portfolio links only.** No invented credentials, no "trusted by" logos.
- **Disclose capability gaps before the CTA.** If a writing task wants an existing audience and you don't have one, the DM says so upfront.
- **Name a price only if the OP stated a budget.** Otherwise ask.
- **No emoji walls, no hype words.** Write like a busy freelancer texts.
- **Hard-reject ToS-adjacent tasks.** Fake reviews, mass account creation, captcha bypass, geo-identity evasion - the agent won't even draft those.

## How the scorer works (no black box)

`fit_scorer.py` is ~150 lines you can read in full:

1. **Hard-reject first:** ToS-skip keywords (from `capability_profile.tos_skips`), rejected payment rails, payout below floor ($5 slavelabour / $20 forhire).
2. **Per-bucket keyword match:** for each of your capability buckets (code/bots/content/visuals/data/security/translation), count keyword hits in the post. Short tokens (≤3 chars like "ts", "etl", "sql") use **word-boundary matching** to avoid false-positives (otherwise "ts" matches "transgender"/"timestamp"). Multi-word phrases use substring match.
3. **`fit_score_1_5 = round(1 + top_score * (strength/5) * 4)`** - combines keyword density with your self-rated strength in that bucket.
4. **`opportunity_0_100 = fit_norm*40 + money_norm*30 + recency*30`** - fit dominates; payout scales; recency decays (1.0 if <6h, 0.7 if <24h, 0.4 otherwise).

The scorer is deterministic: same post + profile = same score, every run. It's fast (hundreds of posts/sec) and free (no model call). For prose polish of the drafted DM, the optional refine-prompt hands off to Claude/ChatGPT.

## Files

```
leads-agent/
  README.md                       (this file)
  LICENSE                         (MIT)
  run_scan.py                     one-shot runner (the entry point)
  fit_scorer.py                   programmatic fit scorer + classify_post()
  dm_drafter.py                   generates DM drafts from scored posts
  capability_profile.example.json template - copy to capability_profile.json
  pitch_profile.example.json      reusable hook library (opener/CTA/disclosures)
  state/                          output dir (gitignored - your scan results)
```

## Honest expectations

This is a real tool, not a magic button. Realistic outcomes for a solo freelancer using it on r/slavelabour + r/forhire:
- **Good weeks:** 2-4 well-fit tasks surfaced, 1-2 DMs sent, 1 lands → $50-400 job.
- **Typical weeks:** 0-2 fits, 0-1 responses.
- **The value:** it replaces 30 min of manual scrolling + drafting per task you pursue, and it catches tasks you'd scroll past. It does NOT guarantee income. The income comes from the work you deliver after the DM lands.

## For non-developers

If you can't or don't want to run Python yourself, the operator offers setup-as-a-service on Fiverr (profile tuning + first scan + recorded walkthrough). The code is the same either way - you just pay for the setup labor. Message before ordering to confirm your niche fits.

## License & attribution

MIT for the code. Use it for your own freelancing, sell it as a service (that's the point), modify it. The `capability_profile.json` you create is yours.

If LeadsAgent helps you land work, a star on this repo is the recommended way to say thanks.

---

Built and dogfooded by [KhangYen](https://github.com/KhangYen) - a data engineer who writes (dbt/SQL/Excel reference packs + pattern repos). Not a course, not a guru - working code.
