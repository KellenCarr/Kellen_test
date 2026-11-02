"""Weekly digest, generated entirely from stored data: ranked asks, ranked problems, blocked cohorts,
what shipped, and pipeline health. Shows Claude's normalized asks and issue links, never issue text."""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from loop import store
from loop.cluster import cluster_items
from loop.llm import llm_call_row

GROUPS = {"asks": ("feature_request", "docs"), "problems": ("bug", "model_behavior")}
TOP_N = 10


def impact(reactions: int, comments: int) -> float:
    """Per-issue impact proxy. Public data has no revenue or seat counts; see docs/design.md."""
    return 1 + reactions + 0.5 * comments


def _latest_triage(conn, repo: str, start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.number, i.html_url, i.reactions, i.comments, c.platform, c.os,
               t.kind, t.area, t.ask, t.blocked, t.severity, t.status, t.review_reason
        FROM issue i
        JOIN cohort c USING (repo, number)
        JOIN triage t ON t.repo = i.repo AND t.number = i.number AND t.content_sha = i.content_sha
        WHERE i.repo = ? AND i.created_at >= ? AND i.created_at < ?
          AND NOT list_contains(?, coalesce(i.author_association, ''))
        QUALIFY row_number() OVER (PARTITION BY i.number ORDER BY t.created_at DESC) = 1
        """,
        [repo, start, end, list(store.STAFF)],
    )
    cols = [d[0] for d in rows.description]
    return [dict(zip(cols, r, strict=True)) for r in rows.fetchall()]


def _links(rows: list[dict], n: int = 5) -> str:
    shown = sorted(rows, key=lambda r: -impact(r["reactions"], r["comments"]))[:n]
    more = f" +{len(rows) - n}" if len(rows) > n else ""
    return ", ".join(f"[#{r['number']}]({r['html_url']})" for r in shown) + more


def _mix(rows: list[dict], key: str) -> str:
    counts = Counter(r[key] for r in rows if r[key] not in ("unknown", None))
    return ", ".join(f"{k} {v}" for k, v in counts.most_common(3)) or "-"


def build_digest(conn, repo: str, window_end: date, *, days: int = 7, client=None, effort: str = "low",
                 run_id: str = "digest") -> str:
    end = datetime.combine(window_end, datetime.min.time(), tzinfo=UTC)
    start = end - timedelta(days=days)
    all_rows = _latest_triage(conn, repo, start, end)
    rows = [r for r in all_rows if r["area"]]  # refusals and schema failures carry no area to rank
    by_number = {r["number"]: r for r in rows}
    out = [f"# Loop digest: {repo}", "",
           f"Issues opened {start:%Y-%m-%d} to {end - timedelta(days=1):%Y-%m-%d}, filed by users (staff excluded). "
           "Ranking uses an impact proxy: 1 + reactions + 0.5 x comments per issue. "
           "Items marked † are awaiting review.", ""]

    for grp, kinds in GROUPS.items():
        clusters = []
        for area in sorted({r["area"] for r in rows if r["kind"] in kinds}):
            items = {r["number"]: r["ask"] for r in rows if r["kind"] in kinds and r["area"] == area}
            if client is not None:
                found, results = cluster_items(client, items, effort)
                for r in results:
                    store.insert_row(conn, "llm_call",
                                     llm_call_row(run_id, "cluster", f"{grp}:{area}", r, datetime.now(UTC)))
            else:
                by_ask = defaultdict(list)
                for n, ask in items.items():
                    by_ask[ask].append(n)
                found = list(by_ask.items())
            for cid, (label, nums) in enumerate(found):
                store.insert_row(conn, "cluster", {"repo": repo, "window_end": window_end, "grp": f"{grp}:{area}",
                                                   "cluster_id": cid, "label": label, "numbers": nums})
                members = [by_number[n] for n in nums]
                score = sum(impact(m["reactions"], m["comments"]) for m in members)
                clusters.append((score, label, area, members))
        clusters.sort(key=lambda c: (-c[0], c[1]))
        title = "What users are asking for" if grp == "asks" else "What is hurting users"
        out += [f"## {title}", "", "| # | Ask | Area | Issues | Impact | Platforms | Examples |",
                "|---|---|---|---|---|---|---|"]
        for i, (score, label, area, members) in enumerate(clusters[:TOP_N], 1):
            flag = " †" if any(m["status"] != "auto" for m in members) else ""
            out.append(f"| {i} | {label}{flag} | {area} | {len(members)} | {score:.0f} | "
                       f"{_mix(members, 'platform')} | {_links(members)} |")
        if not clusters:
            out.append("| - | Nothing this window | | | | | |")
        out.append("")

    blocked = defaultdict(list)
    for r in rows:
        if r["blocked"]:
            blocked[(r["area"], r["platform"], r["os"])].append(r)
    out += ["## Who is blocked", "", "| Area | Platform | OS | Issues | Top asks | Examples |",
            "|---|---|---|---|---|---|"]
    for (area, platform, os_), members in sorted(blocked.items(), key=lambda kv: -len(kv[1]))[:TOP_N]:
        asks = "; ".join(a for a, _ in Counter(m["ask"] for m in members).most_common(2))
        flag = " †" if any(m["status"] != "auto" for m in members) else ""
        out.append(f"| {area} | {platform} | {os_} | {len(members)} | {asks}{flag} | {_links(members, 3)} |")
    if not blocked:
        out.append("| - | | | 0 | Nobody reported blocked | |")
    out.append("")

    releases = conn.execute(
        "SELECT version FROM release WHERE repo = ? AND released_at >= ? AND released_at < ? ORDER BY released_at",
        [repo, start, end]).fetchall()
    versions = [v for (v,) in releases]
    out += ["## What shipped", ""]
    if versions:
        entries = conn.execute(
            "SELECT change_type, surface, text FROM changelog_entry WHERE repo = ? AND list_contains(?, version)",
            [repo, versions]).fetchall()
        types = Counter(t for t, _, _ in entries)
        surfaces = Counter(s for _, s, _ in entries if s)
        out.append(f"{len(versions)} releases ({versions[0]} to {versions[-1]}), {len(entries)} changelog entries: "
                   + ", ".join(f"{k} {v}" for k, v in types.most_common()) + ".")
        if surfaces:
            out.append("Tagged surfaces: " + ", ".join(f"{k} {v}" for k, v in surfaces.most_common(6)) + ".")
        added = [text for t, _, text in entries if t == "added"][:TOP_N]
        if added:
            out += ["", "New capabilities:", *[f"- {a}" for a in added]]
    else:
        out.append("No releases with known dates in this window.")
    out.append("")

    total, auto = len(all_rows), sum(r["status"] == "auto" for r in all_rows)
    reasons = Counter(r["review_reason"] for r in all_rows if r["status"] != "auto")
    cost = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0), count(*) FROM llm_call WHERE purpose = 'triage' "
        "AND list_contains(?, subject)", [[str(r["number"]) for r in all_rows]]).fetchone()
    out += ["## Pipeline health", "",
            f"- Triaged issues in window: {total} ({auto} auto, {total - auto} awaiting review)",
            "- Review reasons: " + (", ".join(f"{k} {v}" for k, v in reasons.most_common()) or "none"),
            f"- Triage spend for these issues: ${cost[0]:.2f} over {cost[1]} calls", ""]
    return "\n".join(out)
