"""SQLite-backed record of every posting the tool has seen and what happened to it."""
import json
import sqlite3
import time
from pathlib import Path

# Status lifecycle:
#   queued        -> supported ATS, passed filters, waiting to be processed
#   filtered      -> failed the config filters (never touched)
#   manual        -> ATS we can't automate (Workday, iCIMS, ...); listed in MANUAL.md
#   needs_review  -> drafted answers waiting for you (`autoapply.py review`)
#   approved      -> you approved the drafts; submitted on the next run / end of review
#   dry_run       -> form filled but not submitted because dry_run is on
#   applied       -> submitted and a confirmation page was detected
#   blocked       -> CAPTCHA / email verification / login wall; listed in MANUAL.md
#   failed        -> something went wrong; see `detail`
#   skipped       -> you chose not to apply during review
#   closed        -> listing went inactive before we got to it
#   duplicate     -> same company + role as another posting (other location/board); `detail` names it
#   ineligible    -> the posting or its form requires a Master's/PhD program
STATUSES = ["queued", "filtered", "manual", "needs_review", "approved", "dry_run",
            "applied", "blocked", "failed", "skipped", "closed", "duplicate", "ineligible"]


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS postings (
                key TEXT PRIMARY KEY,
                repo TEXT, company TEXT, title TEXT, url TEXT, ats TEXT,
                locations TEXT, date_posted INTEGER,
                status TEXT, detail TEXT, attempts INTEGER DEFAULT 0,
                data TEXT, first_seen INTEGER, updated INTEGER)""")
        self.db.commit()

    def get(self, key):
        return self.db.execute("SELECT * FROM postings WHERE key=?", (key,)).fetchone()

    def insert(self, p: dict, status: str, detail: str = ""):
        now = int(time.time())
        self.db.execute(
            "INSERT OR IGNORE INTO postings (key, repo, company, title, url, ats, locations, date_posted,"
            " status, detail, data, first_seen, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["key"], p["repo"], p["company"], p["title"], p["url"], p["ats"],
             ", ".join(p.get("locations") or []), p.get("date_posted"), status, detail, "{}", now, now))
        self.db.commit()

    def set_status(self, key, status, detail="", data=None, bump_attempt=False):
        assert status in STATUSES, status
        sets = "status=?, detail=?, updated=?" + (", attempts=attempts+1" if bump_attempt else "")
        args = [status, detail, int(time.time())]
        if data is not None:
            sets += ", data=?"
            args.append(json.dumps(data))
        self.db.execute(f"UPDATE postings SET {sets} WHERE key=?", (*args, key))
        self.db.commit()

    def by_status(self, *statuses, limit=None):
        q = f"SELECT * FROM postings WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY date_posted DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.db.execute(q, statuses).fetchall()

    def counts(self):
        return dict(self.db.execute("SELECT status, COUNT(*) FROM postings GROUP BY status").fetchall())

    @staticmethod
    def data(row) -> dict:
        return json.loads(row["data"] or "{}")
