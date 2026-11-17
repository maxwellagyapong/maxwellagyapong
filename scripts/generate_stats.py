#!/usr/bin/env python3
"""Generate GitHub profile cards (stats, streak, top languages) that include
private contributions, using your own token via the GitHub GraphQL API.

Public services like github-readme-stats.vercel.app run on *their* token, so
they can never see your private repos. This runs inside your own Action with
your own token instead.

Env: GH_TOKEN (classic PAT with `repo` + `read:user`), optional GH_USER.
Writes: stats.svg, streak.svg, top-langs.svg in the current directory.
"""
import datetime as dt
import html
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("GH_TOKEN") or sys.exit("GH_TOKEN is not set")
API = "https://api.github.com/graphql"

# tokyonight-ish palette
BG, TITLE, TEXT, ICON, ACCENT = "#1a1b27", "#70a5fd", "#38bdae", "#bf91f3", "#ff9e64"


def gql(query, **variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "User-Agent": "profile-stats"},
    )
    with urllib.request.urlopen(req) as r:
        body = json.load(r)
    if body.get("errors"):
        sys.exit(f"GraphQL error: {body['errors']}")
    return body["data"]


def search_count(kind, q):
    """REST search totals. Unlike contributionsCollection, search counts work in
    private org repos (e.g. CLET-GSL-DEV) instead of hiding it as 'restricted'."""
    url = f"https://api.github.com/search/{kind}?per_page=1&q={urllib.parse.quote(q)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "User-Agent": "profile-stats",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)["total_count"]


def fetch_search_stats(login):
    profile_repo = f"{login}/{login}"  # its auto-update commits aren't real work
    year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    return {
        "prs": search_count("issues", f"author:{login} type:pr"),
        "reviews": search_count("issues", f"reviewed-by:{login} type:pr -author:{login}"),
        "issues": search_count("issues", f"author:{login} type:issue"),
        "commits": search_count("commits", f"author:{login} -repo:{profile_repo}"),
        "commits_year": search_count(
            "commits", f"author:{login} -repo:{profile_repo} committer-date:>={year_ago}"
        ),
    }


def fetch_profile():
    return gql(
        """
        query {
          viewer {
            login name createdAt
            followers { totalCount }
            pullRequests { totalCount }
            issues { totalCount }
            repositoriesContributedTo(includeUserRepositories: true,
              contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
            contributionsCollection {
              totalCommitContributions
              restrictedContributionsCount
              totalPullRequestContributions
              totalPullRequestReviewContributions
              contributionCalendar { totalContributions }
            }
          }
        }"""
    )["viewer"]


def fetch_repos():
    """All repos you own or contribute to as org member/collaborator, incl. private."""
    repos, cursor = [], None
    while True:
        data = gql(
            """
            query($cursor: String) {
              viewer {
                repositories(first: 100, after: $cursor, isFork: false,
                  ownerAffiliations: [OWNER, ORGANIZATION_MEMBER, COLLABORATOR]) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    stargazerCount
                    owner { login }
                    languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
                      edges { size node { name color } }
                    }
                  }
                }
              }
            }""",
            cursor=cursor,
        )["viewer"]["repositories"]
        repos += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return repos
        cursor = data["pageInfo"]["endCursor"]


def fetch_bot_commit_days(login, year_start, year_end):
    """Per-day commit counts in the profile repo (the stats bot's auto-updates)."""
    target, out, cursor = f"{login}/{login}", {}, None
    while True:
        repos = gql(
            """
            query($from: DateTime!, $to: DateTime!, $cursor: String) {
              viewer { contributionsCollection(from: $from, to: $to) {
                commitContributionsByRepository(maxRepositories: 100) {
                  repository { nameWithOwner }
                  contributions(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes { occurredAt commitCount }
                  }
                }
              } }
            }""",
            cursor=cursor,
            **{"from": f"{year_start}T00:00:00Z", "to": f"{year_end}T23:59:59Z"},
        )["viewer"]["contributionsCollection"]["commitContributionsByRepository"]
        mine = next((r for r in repos if r["repository"]["nameWithOwner"] == target), None)
        if not mine:
            return out
        for n in mine["contributions"]["nodes"]:
            d = dt.date.fromisoformat(n["occurredAt"][:10])
            out[d] = out.get(d, 0) + n["commitCount"]
        page = mine["contributions"]["pageInfo"]
        if not page["hasNextPage"]:
            return out
        cursor = page["endCursor"]


def fetch_calendar_days(login, created_at):
    """Every day's contribution count since the account was created, minus the
    profile repo's auto-update commits so they don't inflate totals/streaks."""
    start = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
    today = dt.datetime.now(dt.timezone.utc).date()
    days = {}
    year_start = start
    while year_start <= today:
        year_end = min(dt.date(year_start.year, 12, 31), today)
        weeks = gql(
            """
            query($from: DateTime!, $to: DateTime!) {
              viewer { contributionsCollection(from: $from, to: $to) {
                contributionCalendar { weeks { contributionDays { date contributionCount } } }
              } }
            }""",
            **{"from": f"{year_start}T00:00:00Z", "to": f"{year_end}T23:59:59Z"},
        )["viewer"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for w in weeks:
            for d in w["contributionDays"]:
                days[dt.date.fromisoformat(d["date"])] = d["contributionCount"]
        for d, n in fetch_bot_commit_days(login, year_start, year_end).items():
            if d in days:
                days[d] = max(0, days[d] - n)
        year_start = dt.date(year_start.year + 1, 1, 1)
    return dict(sorted(days.items()))


def compute_streaks(days):
    today = max(days)
    total = sum(days.values())
    longest = run = 0
    longest_range = (None, None)
    run_start = None
    for day, count in days.items():
        if count > 0:
            run_start = run_start or day
            run += 1
            if run > longest:
                longest, longest_range = run, (run_start, day)
        else:
            run, run_start = 0, None
    # current streak: today may still be 0 without breaking it
    current, cursor = 0, today
    if days.get(cursor, 0) == 0:
        cursor -= dt.timedelta(days=1)
    end = cursor
    while days.get(cursor, 0) > 0:
        current += 1
        cursor -= dt.timedelta(days=1)
    return {
        "total": total,
        "first": min(days),
        "current": current,
        "current_range": (cursor + dt.timedelta(days=1), end) if current else (None, None),
        "longest": longest,
        "longest_range": longest_range,
    }


def fmt(n):
    return f"{n/1000:.1f}k" if n >= 10_000 else f"{n:,}"


def fmt_date(d):
    return d.strftime("%b %-d, %Y") if d else "—"


def card(width, height, body, title=None):
    head = (
        f'<text x="25" y="35" class="title">{html.escape(title)}</text>' if title else ""
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">
<style>
  .title {{ font: 600 18px 'Segoe UI', Ubuntu, sans-serif; fill: {TITLE}; }}
  .label {{ font: 600 14px 'Segoe UI', Ubuntu, sans-serif; fill: {TEXT}; }}
  .value {{ font: 700 14px 'Segoe UI', Ubuntu, sans-serif; fill: {TEXT}; }}
  .big {{ font: 700 28px 'Segoe UI', Ubuntu, sans-serif; fill: {TEXT}; }}
  .bigaccent {{ font: 700 28px 'Segoe UI', Ubuntu, sans-serif; fill: {ACCENT}; }}
  .sub {{ font: 400 12px 'Segoe UI', Ubuntu, sans-serif; fill: {TEXT}; opacity: .75; }}
  .small {{ font: 400 12px 'Segoe UI', Ubuntu, sans-serif; fill: {TEXT}; }}
</style>
<rect width="100%" height="100%" rx="6" fill="{BG}"/>
{head}
{body}
</svg>
"""


def calculate_rank(commits, prs, issues, reviews, stars, followers):
    """Same formula and grade bands as github-readme-stats (MIT), so the
    grade means the same thing as on the old card. Lower percentile = better."""
    exp_cdf = lambda x: 1 - 2 ** -x
    log_cdf = lambda x: x / (1 + x)
    parts = [  # (weight, score)
        (2, exp_cdf(commits / 250)),  # commits in the last year
        (3, exp_cdf(prs / 50)),
        (1, exp_cdf(issues / 25)),
        (1, exp_cdf(reviews / 2)),
        (4, log_cdf(stars / 50)),
        (1, log_cdf(followers / 10)),
    ]
    pct = 100 * (1 - sum(w * v for w, v in parts) / sum(w for w, _ in parts))
    bands = [(1, "S"), (12.5, "A+"), (25, "A"), (37.5, "A-"), (50, "B+"),
             (62.5, "B"), (75, "B-"), (87.5, "C+"), (100, "C")]
    return next(level for t, level in bands if pct <= t), pct


def rank_ring(level, pct, cx, cy, r=40):
    circ = 2 * 3.14159 * r
    filled = circ * (100 - pct) / 100
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TITLE}" stroke-width="6" opacity=".2"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TITLE}" stroke-width="6" '
        f'stroke-linecap="round" stroke-dasharray="{filled:.1f} {circ:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>'
        f'<text x="{cx}" y="{cy + 9}" class="bigaccent" text-anchor="middle">{level}</text>'
        f'<text x="{cx}" y="{cy + r + 22}" class="sub" text-anchor="middle">Top {pct:.0f}%</text>'
    )


def stats_svg(p, stars, s, contributions_year):
    name = p["name"] or p["login"]
    rows = [
        ("Total contributions (last year)", contributions_year),
        ("Commits (last year)", s["commits_year"]),
        ("Commits (all time)", s["commits"]),
        ("Pull requests (all time)", s["prs"]),
        ("PRs reviewed (all time)", s["reviews"]),
        ("Issues (all time)", s["issues"]),
        ("Repos contributed to", p["repositoriesContributedTo"]["totalCount"]),
        ("Stars earned", stars),
    ]
    body = "".join(
        f'<circle cx="31" cy="{66 + i*24}" r="4" fill="{ICON}"/>'
        f'<text x="45" y="{71 + i*24}" class="label">{html.escape(k)}:</text>'
        f'<text x="400" y="{71 + i*24}" class="value" text-anchor="end">{fmt(v)}</text>'
        for i, (k, v) in enumerate(rows)
    )
    level, pct = calculate_rank(s["commits_year"], s["prs"], s["issues"], s["reviews"],
                                stars, p["followers"]["totalCount"])
    height = 60 + len(rows) * 24 + 10
    body += rank_ring(level, pct, 485, height / 2 - 5)
    return card(560, height, body, f"{name}'s GitHub Stats")


def streak_svg(s):
    cols = [
        (fmt(s["total"]), "Total Contributions", f'{fmt_date(s["first"])} – Present', "big"),
        (str(s["current"]), "Current Streak", " – ".join(map(fmt_date, s["current_range"])) if s["current"] else "—", "bigaccent"),
        (str(s["longest"]), "Longest Streak", " – ".join(map(fmt_date, s["longest_range"])), "big"),
    ]
    body = ""
    for i, (big, label, sub, cls) in enumerate(cols):
        x = 82 + i * 165
        body += (
            f'<text x="{x}" y="70" class="{cls}" text-anchor="middle">{big}</text>'
            f'<text x="{x}" y="105" class="label" text-anchor="middle">{label}</text>'
            f'<text x="{x}" y="128" class="sub" text-anchor="middle">{html.escape(sub)}</text>'
        )
    body += f'<line x1="165" y1="35" x2="165" y2="140" stroke="{ICON}" opacity=".4"/>'
    body += f'<line x1="330" y1="35" x2="330" y2="140" stroke="{ICON}" opacity=".4"/>'
    return card(495, 165, body)


def langs_svg(repos, top=8):
    totals, colors = {}, {}
    for r in repos:
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            totals[n] = totals.get(n, 0) + e["size"]
            colors[n] = e["node"]["color"] or "#858585"
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:top]
    total = sum(v for _, v in ranked) or 1
    bar, x = "", 25.0
    for n, v in ranked:
        w = 330 * v / total
        bar += f'<rect x="{x:.2f}" y="50" width="{w:.2f}" height="8" fill="{colors[n]}"/>'
        x += w
    bar = f'<clipPath id="c"><rect x="25" y="50" width="330" height="8" rx="4"/></clipPath><g clip-path="url(#c)">{bar}</g>'
    items = ""
    for i, (n, v) in enumerate(ranked):
        cx, cy = 25 + (i % 2) * 175, 80 + (i // 2) * 24
        items += (
            f'<circle cx="{cx + 5}" cy="{cy - 4}" r="5" fill="{colors[n]}"/>'
            f'<text x="{cx + 15}" y="{cy}" class="small">{html.escape(n)} {100*v/total:.1f}%</text>'
        )
    rows = (len(ranked) + 1) // 2
    return card(380, 80 + rows * 24, bar + items, "Most Used Languages")


def main():
    profile = fetch_profile()
    repos = fetch_repos()
    login = profile["login"]
    stars = sum(r["stargazerCount"] for r in repos if r["owner"]["login"] == login)
    days = fetch_calendar_days(login, profile["createdAt"])
    streak = compute_streaks(days)
    year_ago = max(days) - dt.timedelta(days=365)
    contributions_year = sum(n for d, n in days.items() if d > year_ago)
    search = fetch_search_stats(login)
    for name, svg in [
        ("stats.svg", stats_svg(profile, stars, search, contributions_year)),
        ("streak.svg", streak_svg(streak)),
        ("top-langs.svg", langs_svg(repos)),
    ]:
        with open(name, "w") as f:
            f.write(svg)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
