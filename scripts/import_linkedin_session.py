#!/usr/bin/env python3
"""
Copy LinkedIn session cookies from your real Chrome profile into the bot profile.
Run this once — after that, the bot profile is permanently logged into LinkedIn.

Usage:
    python scripts/import_linkedin_session.py
"""
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time

CHROME_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")
BOT_PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "runs", "indeed-chrome-profile")
BOT_COOKIES_PATH = os.path.join(BOT_PROFILE_DIR, "Default", "Cookies")
LINKEDIN_HOST = "linkedin.com"


def find_best_source_cookies() -> str:
    """Return the Cookies file from the Chrome profile with the most LinkedIn cookies."""
    candidates = []
    for pattern in ["Default/Cookies", "Profile */Cookies", "Profile*/Cookies"]:
        for path in glob.glob(os.path.join(CHROME_DIR, pattern)):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?",
                    (f"%{LINKEDIN_HOST}",),
                )
                count = cur.fetchone()[0]
                conn.close()
                if count > 0:
                    candidates.append((count, path))
            except Exception:
                continue
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    print(f"  Best source: {candidates[0][1]}  ({candidates[0][0]} LinkedIn cookies)")
    return candidates[0][1]


def get_linkedin_rows(source_path: str) -> list:
    """Read all LinkedIn-related cookie rows from the source DB."""
    conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(cookies)")
    cols = [row[1] for row in cur.fetchall()]
    cur.execute(
        "SELECT * FROM cookies WHERE host_key LIKE ?",
        (f"%{LINKEDIN_HOST}",),
    )
    rows = cur.fetchall()
    conn.close()
    return cols, rows


def ensure_bot_cookies_db(bot_path: str, cols: list) -> None:
    """Create the bot profile cookies DB if it doesn't exist."""
    os.makedirs(os.path.dirname(bot_path), exist_ok=True)
    conn = sqlite3.connect(bot_path)
    cur = conn.cursor()

    # Create table matching the source schema (Chrome keeps it stable)
    col_defs = []
    for col in cols:
        if col in ("creation_utc", "expires_utc", "last_access_utc", "last_update_utc"):
            col_defs.append(f"{col} INTEGER NOT NULL")
        elif col in ("is_secure", "is_httponly", "has_expires", "is_persistent",
                     "priority", "samesite", "source_scheme", "source_port",
                     "is_same_party", "last_update_utc", "source_type",
                     "has_cross_site_ancestor"):
            col_defs.append(f"{col} INTEGER NOT NULL DEFAULT 0")
        else:
            col_defs.append(f"{col} BLOB")
    create_sql = f"CREATE TABLE IF NOT EXISTS cookies ({', '.join(col_defs)})"
    cur.execute(create_sql)
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index "
        "ON cookies (host_key, top_frame_site_key, name, path)"
        if "top_frame_site_key" in cols
        else "CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index "
        "ON cookies (host_key, name, path)"
    )
    conn.commit()
    conn.close()


def import_cookies(source_path: str, bot_path: str) -> int:
    cols, rows = get_linkedin_rows(source_path)
    ensure_bot_cookies_db(bot_path, cols)

    conn = sqlite3.connect(bot_path)
    cur = conn.cursor()

    placeholders = ", ".join("?" * len(cols))
    inserted = 0
    skipped = 0
    for row in rows:
        try:
            cur.execute(
                f"INSERT OR REPLACE INTO cookies ({', '.join(cols)}) VALUES ({placeholders})",
                row,
            )
            inserted += 1
        except Exception as e:
            skipped += 1
    conn.commit()
    conn.close()
    return inserted


def main():
    print("=" * 60)
    print("LinkedIn Session Importer")
    print("=" * 60)
    print()

    # Safety: Chrome must be closed before we touch its cookies DB
    import subprocess
    result = subprocess.run(
        ["pgrep", "-x", "Google Chrome"], capture_output=True, text=True
    )
    if result.returncode == 0:
        print("⚠️  Google Chrome is currently running.")
        print("   Chrome locks the Cookies DB — please QUIT Chrome first.")
        print("   Then re-run this script.")
        print()
        print("   After the import, relaunch Chrome via:")
        print("   bash launch_chrome_bot.sh")
        sys.exit(1)

    print("🔍  Finding Chrome profile with most LinkedIn cookies...")
    source = find_best_source_cookies()
    if not source:
        print("❌  No LinkedIn cookies found in Chrome. Are you logged into LinkedIn?")
        sys.exit(1)

    print(f"📂  Bot profile dir: {os.path.abspath(BOT_PROFILE_DIR)}")
    print(f"🍪  Importing cookies → {BOT_COOKIES_PATH}")

    # Back up existing bot cookies if present
    if os.path.exists(BOT_COOKIES_PATH):
        backup = BOT_COOKIES_PATH + f".bak.{int(time.time())}"
        shutil.copy2(BOT_COOKIES_PATH, backup)
        print(f"    (backed up existing bot cookies to {os.path.basename(backup)})")

    n = import_cookies(source, BOT_COOKIES_PATH)
    print()
    print(f"✅  Imported {n} LinkedIn cookies into bot profile.")
    print()
    print("Next steps:")
    print("  1.  bash launch_chrome_bot.sh   ← opens Chrome bot (already logged in)")
    print("  2.  python -m job_apply_bot linkedin-scan")
    print("  3.  python -m job_apply_bot apply --mode auto")


if __name__ == "__main__":
    main()
