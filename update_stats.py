import io
import os
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ================= CONFIGURATION =================
APKG_PATH = "collection.apkg"
README_PATH = "README.md"
TIMEZONE_OFFSET_HOURS = 1
# =================================================

START_MARKER = "<!-- ANKI_STATS_START -->"
END_MARKER = "<!-- ANKI_STATS_END -->"


def extract_db_from_apkg(apkg_path: str) -> str:
    """Extracts the SQLite database from an .apkg file to a temp file."""
    with zipfile.ZipFile(apkg_path, "r") as zf:
        names = zf.namelist()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
        tmp_path = tmp.name

        if "collection.anki21b" in names:
            try:
                import zstandard as zstd
            except ImportError:
                raise RuntimeError(
                    "Modern Anki .apkg files require 'zstandard'. Run: pip install zstandard"
                )
            raw = zf.read("collection.anki21b")
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(io.BytesIO(raw)) as reader:
                tmp.write(reader.read())
        elif "collection.anki21" in names:
            tmp.write(zf.read("collection.anki21"))
        elif "collection.anki2" in names:
            tmp.write(zf.read("collection.anki2"))
        else:
            raise FileNotFoundError("No Anki database found inside the .apkg file.")

        tmp.close()
        return tmp_path


def format_duration(ms: int) -> str:
    total_minutes = ms // 60000
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def calculate_streaks(active_dates: set, today: datetime.date) -> tuple[int, int]:
    if not active_dates:
        return 0, 0

    sorted_dates = sorted(active_dates)
    longest = 1
    current_run = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            current_run += 1
            longest = max(longest, current_run)
        else:
            current_run = 1

    # Current streak (active today or yesterday)
    current = 0
    check_day = today
    if check_day not in active_dates:
        check_day = today - timedelta(days=1)

    while check_day in active_dates:
        current += 1
        check_day -= timedelta(days=1)

    return current, longest


def generate_markdown(apkg_path: str) -> str:
    db_path = extract_db_from_apkg(apkg_path)
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
    now = datetime.now(tz)
    today = now.date()

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # 1. Load notes -> tags
        note_tags = {}
        cur.execute("SELECT id, tags FROM notes")
        for nid, tags_str in cur.fetchall():
            tags = [t for t in tags_str.strip().split() if t]
            note_tags[nid] = tags if tags else ["Untagged"]

        # 2. Load cards -> status & tag counts
        card_to_tags = {}
        tag_stats = defaultdict(
            lambda: {"total": 0, "mature": 0, "learning": 0, "new": 0, "time_ms": 0}
        )
        total_cards = 0

        cur.execute("SELECT id, nid, type, queue, ivl FROM cards")
        for cid, nid, ctype, queue, ivl in cur.fetchall():
            tags = note_tags.get(nid, ["Untagged"])
            card_to_tags[cid] = tags
            total_cards += 1

            for tag in tags:
                tag_stats[tag]["total"] += 1
                if ctype == 0:
                    tag_stats[tag]["new"] += 1
                elif ctype == 2 and ivl >= 21:
                    tag_stats[tag]["mature"] += 1
                else:
                    tag_stats[tag]["learning"] += 1

        # 3. Load review history (revlog)
        cur.execute("SELECT id, cid, ease, time, type FROM revlog")
        revlogs = cur.fetchall()

        total_reviews = len(revlogs)
        total_time_ms = 0
        daily_time_ms = defaultdict(int)
        daily_reviews = defaultdict(int)
        mature_or_review_total = 0
        mature_or_review_passed = 0

        for rev_id, cid, ease, time_ms, rtype in revlogs:
            dt = datetime.fromtimestamp(rev_id / 1000.0, tz=tz).date()
            total_time_ms += time_ms
            daily_time_ms[dt] += time_ms
            daily_reviews[dt] += 1

            if rtype == 1:  # Review cards (for retention rate)
                mature_or_review_total += 1
                if ease > 1:  # Hard, Good, or Easy
                    mature_or_review_passed += 1

            for tag in card_to_tags.get(cid, ["Untagged"]):
                tag_stats[tag]["time_ms"] += time_ms

        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

    active_days = len(daily_time_ms)
    current_streak, longest_streak = calculate_streaks(set(daily_time_ms.keys()), today)
    avg_daily_active_ms = total_time_ms // active_days if active_days else 0

    last_30_days_ms = sum(
        ms for d, ms in daily_time_ms.items() if (today - d).days < 30
    )
    avg_30d_ms = last_30_days_ms // 30

    retention = (
        (mature_or_review_passed / mature_or_review_total * 100)
        if mature_or_review_total > 0
        else 0.0
    )

    num_languages = len([t for t in tag_stats if t != "Untagged"])

    # Build Markdown output
    lines = [
        "## Anki Language Learning Stats",
        "This file is auto generated by the update_stats.py and is usually added with the PC .apkg file of the collection.",
        "Which takes the learning stats from my PC version, not from mobile where i spend around 99% of time on.",
        "This is mainly only to show how many languages/cards are in the deck and how many i added with time.",
        "Just to confirm -> NO AI has been used in creation of this deck, this is made from my personal experience with languages and im adding cards here every day.",
        "",
        "The formal usually follows: ",
        "{sentence in english / grammar structure in english} + {-language to be translated into-}",
        "{sentence in that language | grammar in that language + usually in commas the spelling of a certain word and in brackets the english translations one by one}",
        "",
        "Now while i would love to be accurate 100% of the time, with aver 25+ languages its not possible for me YET. Thats why for less known ones i do use translators online to help, because if i didnt this thing would take me 30 years to do.",
        "Overall, the pack is free for anyone who wants to check it out and it will be updates basically every day. Normally i spend around 3-4h daily adding Japanese only tbh.",
        "",
        f"*Last updated: {now.strftime('%Y-%m-%d %H:%M')} (UTC{TIMEZONE_OFFSET_HOURS:+d})*",
        "",
        "### Overview",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Languages / Tags** | {num_languages} |",
        f"| **Total Flashcards** | {total_cards:,} |",
        f"| **Total Reviews Completed** | {total_reviews:,} |",
        f"| **Total Time Studied** | {format_duration(total_time_ms)} |",
        f"| **Daily Average (Active Days)** | {format_duration(avg_daily_active_ms)} |",
        f"| **Daily Average (Last 30 Days)** | {format_duration(avg_30d_ms)} |",
        f"| **Active Study Days** | {active_days} days |",
        f"| **Current Streak** | {current_streak} days |",
        f"| **Longest Streak** | {longest_streak} days |",
        f"| **Retention Rate** | {retention:.1f}% |",
        "",
        "### Breakdown by Language / Tag",
        "| Language / Tag | Total Cards | Mature | Learning | New | Time Spent |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]

    # Sort tags by total cards descending
    sorted_tags = sorted(tag_stats.items(), key=lambda x: x[1]["total"], reverse=True)
    for tag, s in sorted_tags:
        lines.append(
            f"| **{tag}** | {s['total']} | {s['mature']} | {s['learning']} | {s['new']} | {format_duration(s['time_ms'])} |"
        )

    # Last 14 days activity chart
    lines.extend(["", "### Last 14 Days Study Time", "```text"])
    last_14 = [today - timedelta(days=i) for i in range(13, -1, -1)]
    max_ms = max((daily_time_ms[d] for d in last_14), default=1) or 1
    for d in last_14:
        ms = daily_time_ms[d]
        mins = ms // 60000
        revs = daily_reviews[d]
        bar_len = int((ms / max_ms) * 25)
        bar = "█" * bar_len + "░" * (25 - bar_len)
        lines.append(f"{d.strftime('%b %d')} | {bar} | {mins:3d} min ({revs} cards)")
    lines.append("```")

    return "\n".join(lines)


def update_readme(readme_path: str, stats_md: str):
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = f"# My Language Learning Journey\n\n{START_MARKER}\n{END_MARKER}\n"

    block = f"{START_MARKER}\n{stats_md}\n{END_MARKER}"

    if START_MARKER in content and END_MARKER in content:
        before = content.split(START_MARKER)[0]
        after = content.split(END_MARKER)[1]
        new_content = before + block + after
    else:
        new_content = content.rstrip() + "\n\n" + block + "\n"

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)


if __name__ == "__main__":
    md = generate_markdown(APKG_PATH)
    update_readme(README_PATH, md)
    print(f"Successfully updated {README_PATH} from {APKG_PATH}!")
