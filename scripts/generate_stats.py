#!/usr/bin/env python3
"""Self-hosted GitHub stats cards for the profile README.

Zero dependencies (Python 3.9+ stdlib only). Queries the GitHub GraphQL API and
renders on-brand SVG cards, in dark and light variants, into ``generated/``:

    stats-{dark,light}.svg      TELEMETRY card: key numbers
    languages-{dark,light}.svg  top languages by bytes (owned, public, non-fork)
    activity-{dark,light}.svg   last-52-weeks contribution heatmap with scan line
    stats.json                  raw numbers (handy for debugging / other tooling)

Environment:
    GITHUB_TOKEN or GH_TOKEN   required, any token that can read public data
                               (a PAT with read:user also counts private contributions)
    GITHUB_USER                default: pedrodcarvalho
    OUTPUT_DIR                 default: <repo>/generated

Failure policy: every SVG is built and XML-validated in memory first. If the
API or rendering fails, the script exits non-zero and leaves the existing files
untouched, so the README never shows a broken card.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from xml.dom import minidom
from xml.sax.saxutils import escape

API_URL = "https://api.github.com/graphql"
USER = os.environ.get("GITHUB_USER", "").strip() or "pedrodcarvalho"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(os.environ.get("OUTPUT_DIR") or ROOT / "generated")
MAX_BYTES = 60_000  # spec: every SVG < 60 KB

# Jupyter notebooks store outputs (plots, base64 images) inline, so their byte
# count dwarfs real source code. Keep the language, but cap its weight at the
# size of the largest non-notebook language so it can tie for first, never
# drown everything else. The raw size is footnoted on the card.
NOTEBOOK_LANGS = {"Jupyter Notebook"}

SANS = "'Geist', 'Inter', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'Geist Mono', 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# Palette copied verbatim from the portfolio CSS (see SPEC.md).
THEMES = {
    "dark": {
        "bg": "#0a0b0d", "elev1": "#111317", "elev2": "#171a1f",
        "line": "rgba(255,255,255,0.08)", "line_strong": "rgba(255,255,255,0.16)",
        "dot": "rgba(255,255,255,0.06)",
        "fg": "#e8eaed", "fg_muted": "#9aa0a6", "fg_subtle": "#8a9097",
        "accent": "#3ddccb", "accent_strong": "#6ff0e0",
        "accent_soft": "rgba(61,220,203,0.12)", "accent_glow": "rgba(61,220,203,0.35)",
        "live": "#ffb547", "live_glow": "rgba(255,181,71,0.4)",
        "grad": ("#3ddccb", "#6ff0e0", "#e8eaed"),
        # heatmap levels 1..4 (level 0 = dot colour)
        "levels": ("#0e4a45", "#167a70", "#26b3a3", "#3ddccb"),
    },
    "light": {
        "bg": "#f6f6f3", "elev1": "#ffffff", "elev2": "#efefeb",
        "line": "rgba(14,16,19,0.09)", "line_strong": "rgba(14,16,19,0.18)",
        "dot": "rgba(14,16,19,0.07)",
        "fg": "#0e1013", "fg_muted": "#4f555c", "fg_subtle": "#5f656c",
        "accent": "#0b7a71", "accent_strong": "#075e57",
        "accent_soft": "rgba(11,122,113,0.10)", "accent_glow": "rgba(11,122,113,0.28)",
        "live": "#8f4f00", "live_glow": "rgba(143,79,0,0.35)",
        "grad": ("#0b7a71", "#075e57", "#0e1013"),
        "levels": ("#b8ece6", "#6fd3c7", "#1fa797", "#0b7a71"),
    },
}

LEVEL_INDEX = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
               "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


class StatsError(RuntimeError):
    """Fatal, user-facing error."""


# --------------------------------------------------------------------------- API

def graphql(token: str, query: str, variables: dict | None = None, retries: int = 3) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    last_err = "unknown error"
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{USER}-profile-stats",
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (401, 403) and "rate limit" not in detail.lower():
                raise StatsError(f"GitHub rejected the token ({last_err})") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = f"network error: {e}"
        else:
            if payload.get("errors"):
                msgs = "; ".join(err.get("message", "?") for err in payload["errors"])
                raise StatsError(f"GraphQL error: {msgs}")
            if not payload.get("data"):
                raise StatsError("GraphQL response had no data")
            return payload["data"]
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise StatsError(f"GitHub API request failed after {retries} attempts ({last_err})")


USER_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    contributionsCollection { contributionYears }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                 first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        stargazerCount
        languages(first: 25, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

COLLECTION_FIELDS = """
  totalCommitContributions
  totalIssueContributions
  totalPullRequestContributions
  totalPullRequestReviewContributions
  restrictedContributionsCount
  contributionCalendar {
    totalContributions
    weeks { contributionDays { date weekday contributionCount contributionLevel } }
  }
"""


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(token: str) -> dict:
    # 1) profile + repositories (paginated)
    repos, after, user = [], None, None
    while True:
        data = graphql(token, USER_QUERY, {"login": USER, "after": after})
        user = data.get("user")
        if user is None:
            raise StatsError(f"user '{USER}' not found")
        page = user["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    # 2) one contributionsCollection per calendar year (API limit: <= 1 year span)
    #    plus the default rolling last-year window, all aliased in one request.
    now = dt.datetime.now(dt.timezone.utc)
    created = dt.datetime.strptime(user["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    parts = [f"last: contributionsCollection {{{COLLECTION_FIELDS}}}"]
    for year in range(created.year, now.year + 1):
        start = max(created, dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc))
        end = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        parts.append(f'y{year}: contributionsCollection(from: "{iso(start)}", to: "{iso(end)}") {{{COLLECTION_FIELDS}}}')
    query = "query($login: String!) { user(login: $login) { %s } }" % "\n".join(parts)
    coll = graphql(token, query, {"login": USER})["user"]

    return {"user": user, "repos": repos, "collections": coll, "now": now}


# ----------------------------------------------------------------------- compute

def days_of(collection: dict) -> list[dict]:
    return [d for w in collection["contributionCalendar"]["weeks"] for d in w["contributionDays"]]


def compute(raw: dict) -> dict:
    user, repos, coll = raw["user"], raw["repos"], raw["collections"]
    years = {k: v for k, v in coll.items() if k.startswith("y")}
    last = coll["last"]

    def total(field: str) -> int:
        return sum(v[field] for v in years.values())

    # Streaks across every day since account creation.
    by_date: dict[str, int] = {}
    for c in years.values():
        for d in days_of(c):
            by_date[d["date"]] = max(by_date.get(d["date"], 0), d["contributionCount"])
    for d in days_of(last):
        by_date[d["date"]] = max(by_date.get(d["date"], 0), d["contributionCount"])
    ordered = [by_date[k] for k in sorted(by_date)]
    active_total = sum(1 for n in ordered if n > 0)
    longest = run = 0
    for n in ordered:
        run = run + 1 if n > 0 else 0
        longest = max(longest, run)
    current, seq = 0, list(ordered)
    if seq and seq[-1] == 0:  # today is not over yet: don't break the streak on it
        seq.pop()
    for n in reversed(seq):
        if n == 0:
            break
        current += 1

    # Languages (bytes), owned public non-fork repos.
    langs, colors = Counter(), {}
    for r in repos:
        for e in r["languages"]["edges"]:
            name = e["node"]["name"]
            langs[name] += e["size"]
            colors[name] = e["node"]["color"] or "#8a9097"
    notebook_raw = {k: langs[k] for k in NOTEBOOK_LANGS if k in langs}
    others = [v for k, v in langs.items() if k not in NOTEBOOK_LANGS]
    cap = max(others) if others else None
    capped = False
    for k, v in notebook_raw.items():
        if cap is not None and v > cap:
            langs[k] = cap
            capped = True

    last_days = days_of(last)
    return {
        "user": USER,
        "generated_at": raw["now"].strftime("%Y-%m-%d"),
        "created_at": user["createdAt"],
        "followers": user["followers"]["totalCount"],
        "repos": len(repos),
        "stars": sum(r["stargazerCount"] for r in repos),
        "contrib_last_year": last["contributionCalendar"]["totalContributions"],
        "contrib_total": sum(v["contributionCalendar"]["totalContributions"] for v in years.values()),
        "commits": total("totalCommitContributions"),
        "prs": total("totalPullRequestContributions"),
        "issues": total("totalIssueContributions"),
        "reviews": total("totalPullRequestReviewContributions"),
        "private": total("restrictedContributionsCount"),
        "active_days_total": active_total,
        "streak_current": current,
        "streak_longest": longest,
        "languages": [{"name": k, "bytes": v, "color": colors[k]} for k, v in langs.most_common()],
        "notebook_raw_bytes": sum(notebook_raw.values()) if capped else 0,
        "calendar": [
            [{"date": d["date"], "count": d["contributionCount"],
              "level": LEVEL_INDEX.get(d["contributionLevel"], 0), "weekday": d["weekday"]}
             for d in w["contributionDays"]]
            for w in last["contributionCalendar"]["weeks"]
        ],
        "active_days_last_year": sum(1 for d in last_days if d["contributionCount"] > 0),
        "best_day_last_year": max((d["contributionCount"] for d in last_days), default=0),
    }


# ----------------------------------------------------------------------- helpers

def t(s) -> str:
    """Escape text content for XML."""
    return escape(str(s))


def fmt(n: int) -> str:
    return f"{n:,}"


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n} B"


def card_open(w: int, h: int, p: dict, title: str, desc: str, extra_css: str, uid: str) -> str:
    g0, g1, g2 = p["grad"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none" role="img" aria-labelledby="{uid}-t {uid}-d">
<title id="{uid}-t">{t(title)}</title>
<desc id="{uid}-d">{t(desc)}</desc>
<defs>
<pattern id="{uid}-dots" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1" fill="{p['dot']}"/></pattern>
<radialGradient id="{uid}-fade" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate({w * 0.12:.0f} 0) rotate(90) scale({h * 1.1:.0f} {w * 0.75:.0f})"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
<mask id="{uid}-m"><rect width="{w}" height="{h}" fill="url(#{uid}-fade)"/></mask>
<linearGradient id="{uid}-disp" x1="0" y1="0" x2="1" y2="0.09"><stop offset="0" stop-color="{g0}"/><stop offset="0.41" stop-color="{g1}"/><stop offset="1" stop-color="{g2}"/></linearGradient>
<clipPath id="{uid}-clip"><rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="16"/></clipPath>
</defs>
<style>
.sans{{font-family:{SANS}}}
.mono{{font-family:{MONO};text-transform:uppercase}}
.eb{{font-size:12px;letter-spacing:.16em;fill:{p['fg_muted']}}}
.sub{{fill:{p['fg_subtle']}}}
.mut{{fill:{p['fg_muted']}}}
.fg{{fill:{p['fg']}}}
.acc{{fill:{p['accent']}}}
.pulse{{transform-box:fill-box;transform-origin:center;animation:pulse 2.4s ease-out infinite}}
@keyframes pulse{{0%{{transform:scale(1);opacity:.85}}70%,100%{{transform:scale(3.2);opacity:0}}}}
.rise{{animation:rise .9s cubic-bezier(.2,.7,.2,1) both}}
@keyframes rise{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
{extra_css}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}.pulse{{opacity:0}}.scan{{display:none}}}}
</style>
<g clip-path="url(#{uid}-clip)">
<rect width="{w}" height="{h}" fill="{p['elev1']}"/>
<rect width="{w}" height="{h}" fill="url(#{uid}-dots)" mask="url(#{uid}-m)"/>
"""


def card_close(w: int, h: int, p: dict) -> str:
    return f"""</g>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="16" stroke="{p['line']}"/>
</svg>
"""


def eyebrow(x: int, y: int, label: str, p: dict) -> str:
    return f"""<circle class="pulse" cx="{x + 4}" cy="{y - 4}" r="4" fill="{p['live_glow']}"/>
<circle cx="{x + 4}" cy="{y - 4}" r="3.5" fill="{p['live']}"/>
<text x="{x + 16}" y="{y}" class="mono eb">{t(label)}</text>"""


# ------------------------------------------------------------------- stats card

def render_stats(s: dict, theme: str) -> str:
    p, W, H, uid = THEMES[theme], 580, 220, "st"
    # Each slot lists (label, key, unit) candidates; the first non-zero one wins,
    # so a vanity metric that is still 0 gets swapped for something meaningful.
    slots = [
        [("Last 365d", "contrib_last_year", "")],
        [("All time", "contrib_total", "")],
        [("Commits", "commits", "")],
        [("Pull reqs", "prs", ""), ("Active days", "active_days_total", "")],
        [("Stars", "stars", ""), ("Followers", "followers", "")],
        [("Repos", "repos", "")],
        [("Streak · now", "streak_current", "d")],
        [("Streak · max", "streak_longest", "d")],
    ]
    cells = []
    for options in slots:
        label, key, unit = next((o for o in options if s[o[1]] > 0), options[0])
        cells.append((label, fmt(s[key]), unit))
    px, top, cw, rh = 28, 62, (W - 56) / 4, 72
    out = [card_open(W, H, p, "GitHub telemetry",
                     f"{USER}: " + ", ".join(f"{a} {b}{c}" for a, b, c in cells), "", uid)]
    out.append(eyebrow(px, 36, "Telemetry · live", p))
    out.append(f'<text x="{W - px}" y="36" text-anchor="end" class="mono eb sub" style="letter-spacing:.12em">@{t(USER)}</text>')
    # grid hairlines
    out.append(f'<path d="M{px} {top}H{W - px}M{px} {top + rh}H{W - px}M{px} {top + 2 * rh}H{W - px}" stroke="{p["line"]}"/>')
    for i in (1, 2, 3):
        x = px + i * cw
        out.append(f'<path d="M{x:.1f} {top + 14}V{top + rh - 14}M{x:.1f} {top + rh + 14}V{top + 2 * rh - 14}" stroke="{p["line"]}"/>')
    for i, (label, value, unit) in enumerate(cells):
        row, col = divmod(i, 4)
        x = px + col * cw + (0 if col == 0 else 16)
        y = top + row * rh
        first = i == 0
        fill = f'url(#{uid}-disp)' if first else p["fg"]
        unit_svg = (f'<tspan dx="3" style="font-family:{MONO};font-size:15px;font-weight:500;fill:{p["fg_subtle"]}">{t(unit)}</tspan>'
                    if unit else "")
        out.append(
            f'<g class="rise" style="animation-delay:{0.15 + i * 0.07:.2f}s">'
            f'<rect x="{x}" y="{y + 18}" width="6" height="2" fill="{p["accent"]}"/>'
            f'<text x="{x + 12}" y="{y + 23}" class="mono sub" style="font-size:11px;letter-spacing:.1em">{t(label)}</text>'
            f'<text x="{x}" y="{y + 56}" class="sans" style="font-size:28px;font-weight:600;letter-spacing:-.02em" fill="{fill}">{t(value)}{unit_svg}</text>'
            f'</g>')
    out.append(card_close(W, H, p))
    return "\n".join(out)


# --------------------------------------------------------------- languages card

def render_languages(s: dict, theme: str) -> str:
    p, W, H, uid = THEMES[theme], 580, 220, "lg"
    langs = s["languages"]
    top = langs[:8]
    total = sum(l["bytes"] for l in langs) or 1
    other = total - sum(l["bytes"] for l in top)
    segs = [(l["name"], l["bytes"] / total, l["color"]) for l in top]
    if other / total >= 0.002:
        segs.append(("Other", other / total, p["line_strong"]))

    px, bar_y, bar_h, bar_w = 28, 60, 12, W - 56
    gap = 2
    css = (".seg{transform-box:fill-box;transform-origin:left center;animation:grow 1s cubic-bezier(.2,.7,.2,1) both}"
           "@keyframes grow{from{transform:scaleX(0)}to{transform:none}}")
    desc = f"Top languages for {USER} by bytes: " + ", ".join(f"{n} {f * 100:.1f}%" for n, f, _ in segs)
    out = [card_open(W, H, p, "Languages", desc, css, uid)]
    out.append(eyebrow(px, 36, "Languages · by bytes", p))
    out.append(f'<text x="{W - px}" y="36" text-anchor="end" class="mono eb sub" style="letter-spacing:.12em">{len(top)} of {len(langs)}</text>')

    # segmented bar
    out.append(f'<clipPath id="{uid}-bar"><rect x="{px}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="6"/></clipPath>')
    out.append(f'<rect x="{px}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="6" fill="{p["dot"]}"/>')
    out.append(f'<g clip-path="url(#{uid}-bar)">')
    x = float(px)
    usable = bar_w - gap * (len(segs) - 1)
    for i, (name, frac, color) in enumerate(segs):
        w = max(usable * frac, 1.5)
        out.append(f'<rect class="seg" style="animation-delay:{0.1 + i * 0.06:.2f}s" x="{x:.2f}" y="{bar_y}" width="{w:.2f}" height="{bar_h}" fill="{color}"/>')
        x += w + gap
    out.append("</g>")

    # legend: 2 columns x 4 rows
    col_w = (W - 2 * px - 32) / 2
    for i, l in enumerate(top):
        col, row = divmod(i, 4)
        lx = px + col * (col_w + 32)
        ly = 106 + row * 23
        pct = l["bytes"] / total * 100
        pct_s = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        out.append(
            f'<g class="rise" style="animation-delay:{0.3 + i * 0.05:.2f}s">'
            f'<circle cx="{lx + 5}" cy="{ly - 5}" r="5" fill="{l["color"]}" stroke="{p["line_strong"]}"/>'
            f'<text x="{lx + 18}" y="{ly}" class="sans fg" style="font-size:14px;font-weight:500">{t(l["name"])}</text>'
            f'<path d="M{lx + 18 + len(l["name"]) * 8.2 + 10:.0f} {ly - 4}H{lx + col_w - 58:.0f}" stroke="{p["line"]}" stroke-dasharray="1 4" stroke-linecap="round"/>'
            f'<text x="{lx + col_w}" y="{ly}" text-anchor="end" class="mono mut" style="font-size:12.5px">{pct_s}</text>'
            f'</g>')
    if s["notebook_raw_bytes"]:
        note = f"Jupyter weight capped · raw {human_bytes(s['notebook_raw_bytes'])} w/ outputs"
    else:
        note = "Owned public repositories · forks excluded"
    rest = len(langs) - len(top)
    out.append(f'<path d="M{px} {H - 40}H{W - px}" stroke="{p["line"]}"/>')
    out.append(f'<text x="{px}" y="{H - 18}" class="mono sub" style="font-size:11px;letter-spacing:.08em">{t(note)}</text>')
    if rest > 0:
        out.append(f'<text x="{W - px}" y="{H - 18}" text-anchor="end" class="mono sub" style="font-size:11px;letter-spacing:.08em">+{rest} more · {other / total * 100:.1f}%</text>')
    out.append(card_close(W, H, p))
    return "\n".join(out)


# ---------------------------------------------------------------- activity card

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def render_activity(s: dict, theme: str) -> str:
    p, W, H, uid = THEMES[theme], 1200, 250, "ac"
    weeks = s["calendar"]
    n = len(weeks)
    gx0, gx1 = 286, W - 36          # heatmap horizontal span
    pitch = (gx1 - gx0) / max(n, 1)
    cell = round(pitch * 0.8, 1)
    gy0 = 92
    gh = 7 * pitch - (pitch - cell)
    scan_dur = 7.0

    css = (
        f".c0{{fill:{p['dot']}}}"
        + "".join(f".c{i + 1}{{fill:{c}}}" for i, c in enumerate(p["levels"]))
        + ".scan{animation:scan %.1fs linear infinite}" % scan_dur
        + "@keyframes scan{from{transform:translateX(0)}to{transform:translateX(%.0fpx)}}" % (gx1 - gx0 + 80)
        + ".hot{animation:hot %.1fs linear infinite}" % scan_dur
        + "@keyframes hot{0%%,100%%{opacity:0}2%%{opacity:1}12%%{opacity:0}}"
    )
    total = s["contrib_last_year"]
    desc = (f"{USER}: {total} contributions in the last year, {s['active_days_last_year']} active days, "
            f"best day {s['best_day_last_year']}, current streak {s['streak_current']} days.")
    out = [card_open(W, H, p, "Contribution activity", desc, css, uid)]
    out.append(eyebrow(36, 40, "Activity · last 52 weeks", p))
    out.append(f'<text x="{W - 36}" y="40" text-anchor="end" class="mono eb sub" style="letter-spacing:.12em">sync · {t(s["generated_at"])} utc</text>')
    out.append(f'<path d="M36 60H{W - 36}" stroke="{p["line"]}"/>')

    # left summary panel
    out.append(f'<g class="rise" style="animation-delay:.1s">'
               f'<text x="34" y="138" class="sans" style="font-size:60px;font-weight:600;letter-spacing:-.035em" fill="url(#{uid}-disp)">{t(fmt(total))}</text>'
               f'<text x="36" y="164" class="mono mut" style="font-size:12px;letter-spacing:.14em">contributions · 365d</text></g>')
    minis = [("Active", f"{s['active_days_last_year']}d"), ("Best day", fmt(s["best_day_last_year"])),
             ("Streak", f"{s['streak_current']}d")]
    for i, (k, v) in enumerate(minis):
        x = 36 + i * 72
        out.append(f'<g class="rise" style="animation-delay:{0.2 + i * 0.07:.2f}s">'
                   f'<text x="{x}" y="198" class="mono sub" style="font-size:10.5px;letter-spacing:.12em">{t(k)}</text>'
                   f'<text x="{x}" y="220" class="sans fg" style="font-size:18px;font-weight:600">{t(v)}</text></g>')
    out.append(f'<path d="M250 84V{gy0 + gh:.0f}" stroke="{p["line"]}"/>')

    # weekday labels
    for wd, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        y = gy0 + wd * pitch + cell / 2 + 4
        out.append(f'<text x="{gx0 - 10}" y="{y:.1f}" text-anchor="end" class="mono sub" style="font-size:10.5px;letter-spacing:.06em">{name}</text>')

    # month labels (first week containing day 1..7 of a month)
    # a month is labelled at the first column whose first day falls in it; the
    # leading partial month is dropped when the next label would collide.
    marks, prev = [], None
    for ci, w in enumerate(weeks):
        m_ = int(w[0]["date"][5:7])
        if m_ != prev:
            marks.append((ci, m_))
            prev = m_
    if len(marks) > 1 and marks[1][0] - marks[0][0] < 3:
        marks.pop(0)
    for ci, m_ in marks:
        out.append(f'<text x="{gx0 + ci * pitch:.1f}" y="{gy0 - 10}" class="mono sub" style="font-size:11px;letter-spacing:.08em">{MONTHS[m_ - 1]}</text>')

    # Cells are <use> references to one shared shape, grouped per week column, so
    # even a fully lit year stays far below the 60 KB budget.
    ys = [f"{gy0 + wd * pitch:.1f}" for wd in range(7)]
    out.append(f'<defs><rect id="{uid}-k" width="{cell}" height="{cell}" rx="3"/>'
               f'<rect id="{uid}-h" x="-1.5" y="-1.5" width="{cell + 3}" height="{cell + 3}" rx="4"/>'
               f'<filter id="{uid}-blur" x="-1" y="-1" width="3" height="3"><feGaussianBlur stdDeviation="3.5"/></filter></defs>')

    # soft teal bloom behind the strongest cells
    glow = [[d for d in w if d["level"] >= 3] for w in weeks]
    if any(glow):
        out.append(f'<g filter="url(#{uid}-blur)" opacity=".8" fill="{p["accent_glow"]}">')
        for ci, ds in enumerate(glow):
            if ds:
                out.append(f'<g transform="translate({gx0 + ci * pitch:.1f} 0)">'
                           + "".join(f'<use href="#{uid}-k" y="{ys[d["weekday"]]}"/>' for d in ds) + "</g>")
        out.append("</g>")

    # cells
    for ci, w in enumerate(weeks):
        out.append(f'<g transform="translate({gx0 + ci * pitch:.1f} 0)">'
                   + "".join(f'<use href="#{uid}-k" y="{ys[d["weekday"]]}" class="c{d["level"]}"/>' for d in w) + "</g>")

    # outline pings on active cells as the scan line passes their column
    out.append(f'<g fill="none" stroke="{p["accent_strong"]}" stroke-width="1.5">')
    for ci, w in enumerate(weeks):
        active = [d for d in w if d["level"] > 0]
        if active:
            delay = ci * pitch / (gx1 - gx0 + 80) * scan_dur
            out.append(f'<g class="hot" style="animation-delay:{delay:.2f}s" opacity="0" transform="translate({gx0 + ci * pitch:.1f} 0)">'
                       + "".join(f'<use href="#{uid}-h" y="{ys[d["weekday"]]}"/>' for d in active) + "</g>")
    out.append("</g>")

    # scan line
    out.append(f'<linearGradient id="{uid}-scan" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="{p["accent"]}" stop-opacity="0"/><stop offset=".85" stop-color="{p["accent"]}" stop-opacity=".16"/><stop offset="1" stop-color="{p["accent_strong"]}" stop-opacity=".5"/></linearGradient>')
    out.append(f'<clipPath id="{uid}-gc"><rect x="{gx0 - 6}" y="{gy0 - 6}" width="{gx1 - gx0 + 12}" height="{gh + 12:.0f}"/></clipPath>')
    out.append(f'<g clip-path="url(#{uid}-gc)"><g class="scan">'
               f'<rect x="{gx0 - 80}" y="{gy0 - 6}" width="80" height="{gh + 12:.0f}" fill="url(#{uid}-scan)"/>'
               f'<rect x="{gx0 - 1}" y="{gy0 - 6}" width="1.5" height="{gh + 12:.0f}" fill="{p["accent_strong"]}" opacity=".9"/>'
               f'</g></g>')

    # legend
    lx = W - 36 - 5 * 16 - 40
    ly = gy0 + gh + 26
    out.append(f'<text x="{lx - 10}" y="{ly + 9}" text-anchor="end" class="mono sub" style="font-size:10.5px;letter-spacing:.1em">less</text>')
    for i in range(5):
        out.append(f'<rect x="{lx + i * 16}" y="{ly}" width="11" height="11" rx="2.5" class="c{i}"/>')
    out.append(f'<text x="{lx + 5 * 16 + 4}" y="{ly + 9}" class="mono sub" style="font-size:10.5px;letter-spacing:.1em">more</text>')
    out.append(card_close(W, H, p))
    return "\n".join(out)


# -------------------------------------------------------------------------- main

def main() -> int:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        print("error: set GITHUB_TOKEN (or GH_TOKEN) to a GitHub token", file=sys.stderr)
        return 2
    try:
        stats = compute(fetch(token))
        files: dict[str, str] = {}
        for theme in THEMES:
            files[f"stats-{theme}.svg"] = render_stats(stats, theme)
            files[f"languages-{theme}.svg"] = render_languages(stats, theme)
            files[f"activity-{theme}.svg"] = render_activity(stats, theme)
        for name, svg in files.items():
            try:
                minidom.parseString(svg.encode("utf-8"))
            except Exception as e:  # noqa: BLE001 - any parse failure is fatal
                raise StatsError(f"{name} is not valid XML: {e}") from None
            size = len(svg.encode("utf-8"))
            if size > MAX_BYTES:
                raise StatsError(f"{name} is {size} bytes (limit {MAX_BYTES})")
        summary = {k: v for k, v in stats.items() if k != "calendar"}
        summary["languages"] = summary["languages"][:12]
        files["stats.json"] = json.dumps(summary, indent=2) + "\n"
    except StatsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError) as e:
        print(f"error: unexpected API response shape ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        tmp = OUT_DIR / f".{name}.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, OUT_DIR / name)
        print(f"wrote {OUT_DIR / name} ({len(content.encode()):,} B)")
    keys = ("contrib_last_year", "contrib_total", "commits", "prs", "issues", "stars",
            "followers", "repos", "streak_current", "streak_longest")
    print(" ".join(f"{k}={stats[k]}" for k in keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
