#!/usr/bin/env python3
"""run_scan.py — one-shot runner for the scan-reddit-tasks pipeline.

Fetches r/slavelabour + r/forhire via opencli, filters to buyers, scores each
against capability_profile.json, ranks, drafts DMs for the top N, writes the
dryrun table to state/reddit_tasks_dryrun.md. NEVER sends a DM (rule 8).

Usage:
  python run_scan.py                          # dry-run (default), top 2, both subs
  python run_scan.py --top 3                  # draft top 3
  python run_scan.py --subreddits slavelabour # one sub only
  python run_scan.py --profile path.json      # client profile (for the gig)
  python run_scan.py --no-draft               # score + rank only, no DM drafts

Exits 0 on success, 1 on fetch failure, 2 if no keepers found.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))
from fit_scorer import score_post, classify_post
from dm_drafter import draft_dm

# Locate opencli (Windows: .cmd; Unix: bare). CWD-independent.
def _find_opencli() -> str:
    for cand in (r"C:\Users\Admin\AppData\Roaming\npm\opencli.cmd", "opencli"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, timeout=10)
            return cand
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("opencli not found on PATH or at the Windows default location.")


def fetch_subreddit(opencli: str, name: str, limit: int = 40) -> list:
    """Fetch newest posts via opencli, return list of post dicts.

    Uses --window foreground (the reliable path) and falls back to background
    only if foreground fails. The skill's original default was background,
    which spawns a detached browser that times out on some Windows setups;
    foreground reuses the visible session and is what actually works here.
    """
    for window in ("foreground", "background"):
        try:
            r = subprocess.run(
                [opencli, "reddit", "subreddit", name, "--sort", "new",
                 "--limit", str(limit), "-f", "json", "--window", window],
                capture_output=True, timeout=120,
                encoding="utf-8", errors="replace",   # opencli emits UTF-8 (emoji, curly quotes); Windows cp1252 default crashes on 0x9d
            )
        except subprocess.TimeoutExpired:
            print(f"[warn] opencli {name} (--window {window}) timed out", file=sys.stderr)
            continue
        if r.returncode != 0:
            print(f"[warn] opencli {name} (--window {window}) failed: {r.stderr[:120]}", file=sys.stderr)
            continue
        try:
            stdout = r.stdout or ""
            data = json.loads(stdout.strip())
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            return data or []
        except json.JSONDecodeError:
            print(f"[warn] {name}: JSON parse failed (stdout len={len(stdout)})", file=sys.stderr)
            return []
    print(f"[warn] opencli {name}: both foreground and background failed", file=sys.stderr)
    return []


def extract_payout(title: str, body: str) -> int | None:
    m = re.search(r"\$(\d+)", title) or re.search(r"\$(\d+)", body)
    return int(m.group(1)) if m else None


def run(top_n: int, subreddits: list, profile_path: Path, draft: bool, state_dir: Path):
    opencli = _find_opencli()
    caps = json.loads(profile_path.read_text(encoding="utf-8"))
    pitch_path = profile_path.parent / "pitch_profile.json"
    pitch = json.loads(pitch_path.read_text(encoding="utf-8")) if pitch_path.exists() else {}

    now = time.time()
    all_keepers, all_rejects, total_buyers, total_sellers = [], [], 0, 0

    for sub in subreddits:
        print(f"=== r/{sub} ===")
        posts = fetch_subreddit(opencli, sub)
        print(f"  fetched {len(posts)} posts")
        for p in posts:
            kind = classify_post(p.get("title", ""), p.get("selftext", ""))
            if kind == "seller":
                total_sellers += 1; continue
            if kind != "buyer":
                continue
            total_buyers += 1
            post = {
                "id": p.get("id"), "title": p.get("title", ""),
                "selftext": (p.get("selftext") or "")[:600],
                "subreddit": sub, "author": p.get("author", ""),
                "url": p.get("url", ""),
                "payout_usd": extract_payout(p.get("title", ""), p.get("selftext", "")),
                "rail": "", "created_utc": p.get("created_utc", now),
            }
            r = score_post(post, caps)
            post.update({"_fit": r.fit_score_1_5, "_bucket": r.top_bucket,
                         "_opp": r.opportunity_0_100, "_reject": r.reject_reason,
                         "_matched": r.matched_keywords[:5]})
            if r.reject_reason:
                all_rejects.append(post)
            elif r.fit_score_1_5 >= 2:
                all_keepers.append(post)

    all_keepers.sort(key=lambda c: c["_opp"], reverse=True)

    # --- Dryrun table ---
    state_dir.mkdir(parents=True, exist_ok=True)
    table_path = state_dir / "reddit_tasks_dryrun.md"
    lines = ["# Reddit tasks dry-run — " + time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
             "", f"Scanned {sum(len(fetch_subreddit(opencli, s, 0) if False else []) for s in [])}... ",
             f"Sellers filtered: {total_sellers} | Buyers: {total_buyers} | Keepers: {len(all_keepers)}",
             "",
             "| rank | subreddit | title | payout | fit | opp | bucket | url |",
             "|------|-----------|-------|--------|-----|-----|--------|-----|"]
    for i, c in enumerate(all_keepers[:10], 1):
        lines.append(f"| {i} | r/{c['subreddit']} | {c['title'][:50]} | ${c['payout_usd'] or '?'} | {c['_fit']}/5 | {c['_opp']} | {c['_bucket']} | [link]({c['url']}) |")
    table_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n=== SUMMARY ===")
    print(f"  sellers removed: {total_sellers}")
    print(f"  buyers scored:   {total_buyers}")
    print(f"  keepers (fit≥2): {len(all_keepers)}")

    if not all_keepers:
        print("\nNo keepers this window. (Table written to state/reddit_tasks_dryrun.md)")
        return 2

    print(f"\n=== TOP {min(top_n, len(all_keepers))} KEEPERS ===")
    for i, c in enumerate(all_keepers[:top_n], 1):
        age_h = (now - c["created_utc"]) / 3600
        print(f"  #{i} fit={c['_fit']}/5 opp={c['_opp']:5.1f} ${str(c['payout_usd'] or '?'):>4} [{age_h:4.1f}h] {c['_bucket']:8s} {c['title'][:55]}")
        print(f"       matched: {', '.join(c['_matched']) or '(none)'}")

    if draft:
        print(f"\n=== DRAFTING DMs for top {top_n} ===")
        for c in all_keepers[:top_n]:
            r = score_post(c, caps)
            d = draft_dm(c, r, caps, pitch)
            flag = "OVER" if d.over_limit else "ok"
            print(f"  wrote {d.filepath.name} | {d.word_count}w {flag} | bucket={c['_bucket']}")
            print(f"     -> review, then paste in your own browser (rule 8)")

    print(f"\nTable: {table_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan Reddit for tasks matching the capability profile.")
    ap.add_argument("--top", type=int, default=2, help="draft DMs for top N (default 2)")
    ap.add_argument("--subreddits", default="slavelabour,forhire", help="comma-separated")
    ap.add_argument("--profile", type=Path, default=THIS_DIR / "capability_profile.json")
    ap.add_argument("--no-draft", action="store_true", help="score + rank only")
    ap.add_argument("--state-dir", type=Path, default=THIS_DIR.parents[2] / "state")
    args = ap.parse_args()
    sys.exit(run(
        top_n=args.top,
        subreddits=[s.strip() for s in args.subreddits.split(",")],
        profile_path=args.profile,
        draft=not args.no_draft,
        state_dir=args.state_dir,
    ))
