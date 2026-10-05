#!/usr/bin/env python3
"""Auto-apply to internship postings from GitHub lists.

  autoapply.py run              fetch repos, apply to new postings (what the schedule runs)
  autoapply.py review           go through drafted answers that need your OK, then submit
  autoapply.py status           counts per status + where the reports are
  autoapply.py try URL          fill one application in a visible browser without submitting
  autoapply.py install-schedule / uninstall-schedule   macOS launchd job
"""
import argparse
import fcntl
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import drafting  # noqa: E402
import forms  # noqa: E402
import sources  # noqa: E402
from store import Store  # noqa: E402

SUPPORTED_ATS = {"greenhouse", "lever", "ashby"}
# Postings that stand for their role, so later listings of the same role are duplicates.
ROLE_STATUSES = ("queued", "manual", "needs_review", "approved", "dry_run", "applied", "blocked", "failed", "skipped")
PLIST = Path.home() / "Library/LaunchAgents/com.internship-autoapply.plist"


# ---------------------------------------------------------------- setup

def load_config() -> dict:
    path = Path(os.environ.get("AUTOAPPLY_CONFIG", ROOT / "config.yaml"))
    if not path.exists():
        sys.exit(f"No config.yaml yet. Copy config.example.yaml to {path} and fill it in.")
    cfg = yaml.safe_load(path.read_text())
    cfg["resume_pdf"] = Path(os.path.expanduser(cfg["resume_pdf"]))
    if not cfg["resume_pdf"].exists():
        sys.exit(f"resume_pdf not found: {cfg['resume_pdf']}")
    cl = cfg.get("cover_letter_pdf")
    cfg["cover_letter_pdf"] = Path(os.path.expanduser(cl)) if cl else None
    for k in ("first_name", "last_name", "email", "phone"):
        if not str(cfg["profile"].get(k) or "").strip() or "YOUR" in str(cfg["profile"][k]):
            sys.exit(f"Fill in profile.{k} in config.yaml")
    return cfg


def resume_text(cfg) -> str:
    if cfg.get("resume_text_file"):
        return Path(os.path.expanduser(cfg["resume_text_file"])).read_text()
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(cfg["resume_pdf"]).pages).strip()


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title, msg):
    if sys.platform == "darwin":
        msg, title = msg.replace('"', "'"), title.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                       capture_output=True)


class Browser:
    """Persistent Chrome profile so cookies/CAPTCHA reputation carry across runs."""

    def __init__(self, headless):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        kw = dict(user_data_dir=str(ROOT / "browser_profile"), headless=headless,
                  viewport={"width": 1280, "height": 900})
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(channel="chrome", **kw)
        except Exception:
            self.ctx = self.pw.chromium.launch_persistent_context(**kw)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self):
        self.ctx.close()
        self.pw.stop()


# ---------------------------------------------------------------- core

def description_url(url: str) -> str:
    return re.sub(r"/(apply|application)(?=$|\?)", "", url)


def remap_answers(old_fields, old_answers, new_fields):
    """Carry answers over to a fresh scan of the same form (keys are reassigned each scan)."""
    pool = {}
    for f in old_fields:
        if f["key"] in old_answers:
            pool.setdefault((f["kind"], f["label"]), []).append(old_answers[f["key"]])
    out = {}
    for f in new_fields:
        lst = pool.get((f["kind"], f["label"]))
        if lst:
            out[f["key"]] = lst.pop(0)
    return out


def process(br: Browser, store: Store, row, cfg, resume: str, interactive=False):
    """Draft (or reuse approved) answers, fill, and submit one application."""
    page, key = br.page, row["key"]
    url = sources.normalize_url(row["url"])
    shots = ROOT / "screenshots"
    shots.mkdir(exist_ok=True)
    data = Store.data(row)
    approved = row["status"] == "approved"

    try:
        if not approved:
            desc_url = description_url(url)
            if desc_url != url:
                page.goto(desc_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
            description = forms.page_text(page) if desc_url != url else ""
        forms.open_form(page, url)
        if approved:
            description = data.get("description", "")
        else:
            description = description or forms.page_text(page)
        fields = forms.scan(page)
        if not any(f["kind"] == "file" for f in fields) or len(fields) < 3:
            store.set_status(key, "blocked", "no application form found (closed posting or login wall?)",
                             bump_attempt=True)
            return

        job = {"company": row["company"], "title": row["title"], "description": description}
        if approved:
            answers = remap_answers(data["fields"], data["answers"], fields)
        else:
            answers, ineligible = drafting.draft_answers(cfg["profile"] | {"eeo": cfg.get("eeo", {})}, resume, job,
                                                         fields, cfg.get("llm_backend", "auto"))
            if ineligible:
                store.set_status(key, "ineligible", ineligible[:300])
                promote_duplicate(store, key)
                log(f"  -> ineligible: {ineligible}")
                return
            flagged = drafting.needs_review(fields, answers)
            if flagged:
                store.set_status(key, "needs_review", f"{len(flagged)} answer(s) to review", data={
                    "fields": fields, "answers": answers, "flagged": flagged, "description": description[:6000]})
                log(f"  -> needs review ({len(flagged)} flagged)")
                return

        problems = forms.fill(page, fields, answers, cfg["resume_pdf"], cfg["cover_letter_pdf"])
        record = {"fields": fields, "answers": answers, "description": description[:6000]}
        if problems:
            # Usually an answer that didn't match the form's options; let a human sort it out.
            page.screenshot(path=str(shots / f"{key}-problem.png"), full_page=True)
            store.set_status(key, "needs_review", "; ".join(problems)[:500],
                             data=record | {"flagged": data.get("flagged", []), "problems": problems})
            log(f"  -> fill problems, sent to review: {problems[:3]}")
            return
        if cfg.get("dry_run", True) and not interactive:
            page.screenshot(path=str(shots / f"{key}-dry_run.png"), full_page=True)
            store.set_status(key, "dry_run", "filled, not submitted (dry_run: true)", data=record)
            log("  -> dry run: filled, not submitted")
            return
        status, detail = forms.submit(page, wait_for_human=180 if interactive else 0)
        page.screenshot(path=str(shots / f"{key}-{status}.png"), full_page=True)
        store.set_status(key, status, detail, data=record, bump_attempt=True)
        log(f"  -> {status}: {detail}")
    except Exception as e:  # keep going with the rest of the queue
        store.set_status(key, "failed", f"{type(e).__name__}: {str(e).splitlines()[0][:300]}", bump_attempt=True)
        log(f"  -> failed: {e}")


def promote_duplicate(store: Store, key: str):
    """The kept posting for a role was dropped; queue its next duplicate (e.g. another location) instead."""
    dups = [r for r in store.by_status("duplicate") if r["detail"] == f"same role as {key}"]
    if not dups:
        return
    first = dups[0]
    store.set_status(first["key"], "queued" if first["ats"] in SUPPORTED_ATS else "manual",
                     "" if first["ats"] in SUPPORTED_ATS else f"{first['ats']} isn't automated")
    for r in dups[1:]:
        store.set_status(r["key"], "duplicate", f"same role as {first['key']}")


def _home_terms(profile: dict) -> list[str]:
    parts = [profile.get(k, "") for k in ("city", "state_or_province", "country")]
    parts += (profile.get("location") or "").split(",")
    return [t.strip().lower() for t in parts if t.strip()]


def ingest(store: Store, cfg) -> int:
    new = 0
    live = set()
    # One application per role: postings that repeat a company + title (other locations, other boards)
    # are recorded as duplicates of the first one kept.
    roles = {sources.role_key(r): r["key"] for r in store.by_status(*ROLE_STATUSES)}
    home = _home_terms(cfg.get("profile") or {})

    def nearness(p):  # keep the location closest to the candidate (e.g. Toronto over the US)
        locs = " ".join(p.get("locations") or []).lower()
        return 2 if any(t in locs for t in home) else 1 if "remote" in locs else 0

    for spec in cfg["repos"]:
        try:
            postings = sources.fetch_repo(spec)
        except Exception as e:
            log(f"could not fetch {spec}: {e}")
            continue
        log(f"{spec}: {len(postings)} active postings")
        for p in sorted(postings, key=nearness, reverse=True):
            live.add(p["key"])
            if store.get(p["key"]):
                continue
            ok, why = sources.passes_filters(p, cfg.get("filters") or {})
            rk = sources.role_key(p)
            if not ok:
                store.insert(p, "filtered", why)
            elif rk in roles:
                store.insert(p, "duplicate", f"same role as {roles[rk]}")
            elif p["ats"] not in SUPPORTED_ATS:
                store.insert(p, "manual", f"{p['ats']} isn't automated")
                roles[rk] = p["key"]
            else:
                store.insert(p, "queued")
                roles[rk] = p["key"]
                new += 1
    for row in store.by_status("queued"):  # delisted before we got to it
        if row["key"] not in live:
            store.set_status(row["key"], "closed", "no longer listed as active")
    return new


def write_reports(store: Store):
    lines = ["# Applications waiting for your review", "",
             "Run `./autoapply.py review` to approve/edit these (or skip them).", ""]
    for row in store.by_status("needs_review"):
        d = Store.data(row)
        fields = {f["key"]: f for f in d.get("fields", [])}
        lines += [f"## {row['company']} — {row['title']}", f"{row['url']}", ""]
        for p in d.get("problems", []):
            lines.append(f"- ⚠️ fill problem: {p}")
        for k in d.get("flagged", []):
            a = d["answers"].get(k, {})
            lines += [f"**{fields.get(k, {}).get('label', k)}**",
                      f"> {a.get('value') or ', '.join(a.get('values') or []) or '(no answer)'}",
                      *( [f"_note: {a['note']}_"] if a.get("note") else []), ""]
    (ROOT / "REVIEW.md").write_text("\n".join(lines))

    lines = ["# Apply to these by hand", "",
             "Unsupported job sites (Workday, iCIMS, ...) and applications blocked by a CAPTCHA "
             "or email verification.", "", "| Posted | Company | Role | Why | Link |", "|---|---|---|---|---|"]
    for row in store.by_status("blocked", "manual", limit=400):
        posted = datetime.fromtimestamp(row["date_posted"]).strftime("%b %d") if row["date_posted"] else ""
        lines.append(f"| {posted} | {row['company']} | {row['title']} | {row['detail']} | [apply]({row['url']}) |")
    (ROOT / "MANUAL.md").write_text("\n".join(lines))


# ---------------------------------------------------------------- commands

def cmd_run(args):
    cfg = load_config()
    lock = open(ROOT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another run is in progress")
    store = Store(ROOT / "state.db")
    new = ingest(store, cfg)
    log(f"{new} new posting(s) queued")
    retry_dry = [] if cfg.get("dry_run", True) else store.by_status("dry_run")
    retry_failed = [r for r in store.by_status("failed") if r["attempts"] < 2]
    todo = store.by_status("approved") + store.by_status("queued") + retry_dry + retry_failed
    todo = todo[: cfg.get("max_applications_per_run", 20)]
    if todo:
        resume = resume_text(cfg)
        br = Browser(cfg.get("headless", False))
        try:
            for i, row in enumerate(todo):
                log(f"[{i + 1}/{len(todo)}] {row['company']} — {row['title']} ({row['status']})")
                process(br, store, row, cfg, resume)
                # Pace real submissions so they look less bot-like; dry runs and held drafts submit nothing.
                if store.get(row["key"])["status"] in ("applied", "failed"):
                    time.sleep(random.uniform(8, 25))
        finally:
            br.close()
    write_reports(store)
    c = store.counts()
    summary = f"applied {c.get('applied', 0)} total · {c.get('needs_review', 0)} waiting for review"
    log(summary + f" · counts: {c}")
    if c.get("needs_review"):
        notify("Internship auto-apply", f"{c['needs_review']} application(s) need your review. "
                                        "Run: autoapply.py review")


def _edit(text: str) -> str:
    import tempfile
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as f:
        f.write(text)
    editor = os.environ.get("EDITOR") or "nano"
    subprocess.call([*editor.split(), f.name])
    return Path(f.name).read_text()


def cmd_review(args):
    cfg = load_config()
    store = Store(ROOT / "state.db")
    rows = store.by_status("needs_review")
    if not rows:
        print("Nothing to review.")
    for n, row in enumerate(rows, 1):
        d = Store.data(row)
        fields = {f["key"]: f for f in d["fields"]}
        flagged = d.get("flagged") or []
        while True:
            print("\n" + "=" * 80)
            print(f"[{n}/{len(rows)}] {row['company']} — {row['title']}\n{row['url']}")
            for p in d.get("problems", []):
                print(f"  ⚠️  {p}")
            for k in flagged:
                f, a = fields[k], d["answers"].get(k, {})
                print(f"\n  ❓ {f['label']}" + ("  (required)" if f["required"] else ""))
                if f.get("options"):
                    print(f"     options: {f['options'][:12]}")
                if a.get("note"):
                    print(f"     note: {a['note']}")
                ans = a.get("value") or ", ".join(a.get("values") or []) or "(empty)"
                print("     " + ans.replace("\n", "\n     "))
            c = input("\n[a]pprove  [e]dit flagged  [E]dit all  [v]iew all  [o]pen  [s]kip job  [n]ext  [q]uit > ").strip()
            if c in ("e", "E"):
                keys = flagged if c == "e" else [k for k in fields if fields[k]["kind"] != "file"]
                doc = [{"key": k, "question": fields[k]["label"], "options": fields[k].get("options"),
                        "answer": d["answers"].get(k, {}).get("values") or d["answers"].get(k, {}).get("value", "")}
                       for k in keys]
                edited = yaml.safe_load(_edit("# Edit the `answer` lines. Lists are for multi-select.\n"
                                              + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)))
                for item in edited or []:
                    ans = item.get("answer")
                    a = d["answers"].setdefault(item["key"], {"kind": "factual", "note": ""})
                    a["values"], a["value"] = (ans, "") if isinstance(ans, list) else ([], str(ans or ""))
                    a["confident"] = True
            elif c == "v":
                for k, f in fields.items():
                    if f["kind"] == "file":
                        continue
                    a = d["answers"].get(k, {})
                    print(f"  {f['label'][:60]:<60} | {(a.get('value') or a.get('values') or '')!s:.70}")
            elif c == "o":
                subprocess.run(["open", row["url"]])
            elif c == "a":
                d["problems"] = []
                store.set_status(row["key"], "approved", "approved in review", data=d)
                break
            elif c == "s":
                store.set_status(row["key"], "skipped", "skipped in review")
                break
            elif c == "n":
                break
            elif c == "q":
                rows = rows[:n]
                break
        if c == "q":
            break

    approved = store.by_status("approved")
    write_reports(store)
    if approved and input(f"\nSubmit {len(approved)} approved application(s) now? A Chrome window opens; "
                          "solve any CAPTCHA that appears. [Y/n] ").strip().lower() != "n":
        resume = resume_text(cfg)
        br = Browser(headless=False)
        try:
            for row in approved:
                log(f"{row['company']} — {row['title']}")
                process(br, store, row, cfg, resume, interactive=True)
        finally:
            br.close()
        write_reports(store)


def cmd_status(args):
    store = Store(ROOT / "state.db")
    for k, v in sorted(store.counts().items(), key=lambda kv: -kv[1]):
        print(f"  {k:<13} {v}")
    print(f"\nReview queue: {ROOT / 'REVIEW.md'}\nApply by hand: {ROOT / 'MANUAL.md'}")
    for row in store.by_status("applied", "failed", limit=args.recent):
        print(f"  {row['status']:<8} {row['company']} — {row['title']}  {row['detail'][:80]}")


def cmd_try(args):
    """Fill one form in a visible browser without submitting, to check the result."""
    cfg = load_config()
    store = Store(ROOT / "state.db")
    url = args.url
    ats = sources.detect_ats(url)
    if ats not in SUPPORTED_ATS:
        sys.exit(f"{ats} isn't supported")
    p = {"key": sources.posting_key(url), "repo": "manual", "company": args.company or "", "title": args.title or "",
         "url": url, "ats": ats}
    store.insert(p, "queued")
    cfg["dry_run"] = True
    br = Browser(headless=False)
    try:
        process(br, store, store.get(p["key"]), cfg, resume_text(cfg))
        row = store.get(p["key"])
        print(f"\nstatus: {row['status']} — {row['detail']}")
        if row["status"] == "needs_review":
            print("Drafted answers need review; run `autoapply.py review`.")
        input("Look over the browser window, then press Enter to close it. Nothing was submitted. ")
    finally:
        br.close()


def cmd_install_schedule(args):
    cfg = load_config()
    hours = cfg.get("schedule_hours", [9, 17])
    py = ROOT / ".venv/bin/python"
    (ROOT / "logs").mkdir(exist_ok=True)
    intervals = "".join(f"<dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>0</integer></dict>"
                        for h in hours)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.internship-autoapply</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{ROOT / 'autoapply.py'}</string><string>run</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>StartCalendarInterval</key><array>{intervals}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StandardOutPath</key><string>{ROOT / 'logs/run.log'}</string>
  <key>StandardErrorPath</key><string>{ROOT / 'logs/run.log'}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    print(f"Scheduled daily at {', '.join(f'{h}:00' for h in hours)}. Log: {ROOT / 'logs/run.log'}")


def cmd_uninstall_schedule(args):
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Schedule removed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    s = sub.add_parser("status")
    s.add_argument("--recent", type=int, default=15)
    s.set_defaults(fn=cmd_status)
    t = sub.add_parser("try")
    t.add_argument("url")
    t.add_argument("--company")
    t.add_argument("--title")
    t.set_defaults(fn=cmd_try)
    sub.add_parser("install-schedule").set_defaults(fn=cmd_install_schedule)
    sub.add_parser("uninstall-schedule").set_defaults(fn=cmd_uninstall_schedule)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
