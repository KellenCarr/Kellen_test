"""loop ingest | triage | digest"""

import argparse
import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from loop import store
from loop.changelog import parse_changelog, parse_release_feed
from loop.github import GitHub


def _date(s: str) -> datetime:
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=UTC)


def cmd_ingest(args) -> None:
    conn = store.connect(args.db)
    gh = GitHub(token=os.environ.get("GITHUB_TOKEN"))
    since = _date(args.since) if args.since else conn.execute(
        "SELECT max(updated_at) FROM issue WHERE repo = ?", [args.repo]).fetchone()[0]
    since = since or datetime.now(UTC) - timedelta(days=7)
    now, n = datetime.now(UTC), 0
    for item in gh.iter_issues(args.repo, since=since):
        store.upsert_issue(conn, args.repo, item, now)
        n += 1
    print(f"issues: {n} upserted (updated since {since:%Y-%m-%d %H:%M})")
    if args.changelog:
        entries = parse_changelog(gh.get_file(args.repo, args.changelog))
        store.upsert_changelog(conn, args.repo, entries)
        print(f"changelog: {len(entries)} entries")
    if args.feed:
        releases = parse_release_feed(gh.get_file(args.repo, args.feed))
        store.upsert_releases(conn, args.repo, releases)
        print(f"releases: {len(releases)} dated")


def cmd_triage(args) -> None:
    from loop.llm import make_client
    from loop.triage import Taxonomy, run_triage

    conn = store.connect(args.db)
    s = run_triage(conn, make_client(), args.repo, _date(args.since), effort=args.effort, limit=args.limit,
                   max_cost=args.max_cost, workers=args.workers, run_id=uuid.uuid4().hex[:12],
                   taxonomy=Taxonomy.load(Path(args.taxonomy)) if args.taxonomy else None)
    print(f"candidates {s.candidates} | triaged {s.triaged} ({s.needs_review} need review) | failed {s.failed} "
          f"| ${s.cost_usd:.2f}" + (" | STOPPED: budget reached" if s.stopped_on_budget else ""))


def cmd_digest(args) -> None:
    from loop.digest import build_digest

    client = None
    if not args.no_cluster:
        from loop.llm import make_client
        client = make_client()
    conn = store.connect(args.db)
    end = date.fromisoformat(args.week_ending) if args.week_ending else datetime.now(UTC).date()
    md = build_digest(conn, args.repo, end, days=args.days, client=client, effort=args.effort,
                      run_id=uuid.uuid4().hex[:12])
    out = Path(args.out) / f"digest-{args.repo.replace('/', '-')}-{end}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(out)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loop")
    p.add_argument("--db", default="data/loop.duckdb")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="fetch issues, changelog and release dates")
    i.add_argument("--repo", required=True)
    i.add_argument("--since", help="YYYY-MM-DD; default: last ingested update, else 7 days ago")
    i.add_argument("--changelog", default="CHANGELOG.md", help="path in the repo; '' to skip")
    i.add_argument("--feed", default="feed.xml", help="Atom release feed path in the repo; '' to skip")
    i.set_defaults(fn=cmd_ingest)

    t = sub.add_parser("triage", help="Claude triage of untriaged issues")
    t.add_argument("--repo", required=True)
    t.add_argument("--since", required=True, help="YYYY-MM-DD: triage issues created on or after")
    t.add_argument("--limit", type=int)
    t.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    t.add_argument("--max-cost", type=float, default=2.0, help="USD; stop submitting work at this spend")
    t.add_argument("--workers", type=int, default=4)
    t.add_argument("--taxonomy")
    t.set_defaults(fn=cmd_triage)

    d = sub.add_parser("digest", help="write the weekly digest")
    d.add_argument("--repo", required=True)
    d.add_argument("--week-ending", help="YYYY-MM-DD, exclusive; default today")
    d.add_argument("--days", type=int, default=7)
    d.add_argument("--no-cluster", action="store_true", help="skip Claude clustering (exact-ask grouping only)")
    d.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    d.add_argument("--out", default="out")
    d.set_defaults(fn=cmd_digest)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
