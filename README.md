# internship-autoapply

Watches GitHub internship lists (SimplifyJobs and similar) and applies to new postings with your resume.

- **Only standard questions** (contact info, work authorization, EEO, GPA, ...): answered from your profile and submitted automatically.
- **Open-ended questions** ("Why X?", "Tell me about a time...") or anything it isn't sure of: it drafts an answer from your resume and the job posting, then holds the application for your review. You get a macOS notification, and the drafts are written to `REVIEW.md`.
- **Roles you can't hold** (Master's/PhD-only, high-school-only, US-citizens-only): skipped as `ineligible`.
- **Job sites it can't automate** (Workday, iCIMS, Oracle, company sites) and applications blocked by a CAPTCHA go into `MANUAL.md` with links, so you can apply by hand.

Supported job sites: Greenhouse (including company career pages that embed Greenhouse via `?gh_jid=`), Lever and Ashby. On the Summer 2027 Simplify list that's roughly a quarter of software/ML postings. Workday is over half, and it needs a separate account and email verification for each company, so it isn't automated.

## Setup

Needs macOS, Google Chrome, Python 3.12+ and Claude Code (the `claude` CLI, 2.1.280+).

```bash
git clone <this repo> ~/internship-autoapply && cd ~/internship-autoapply
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # fallback if Chrome isn't installed

cp config.example.yaml config.yaml      # then fill it in (see below)
```

Drafting uses your Claude Code login through the `claude` CLI. To use an API key instead, set `ANTHROPIC_API_KEY` and `llm_backend: api`.

### Filling in config.yaml

- `resume_pdf`: path to your resume.
- `profile`: contact details, school, GPA, graduation date, links.
- `profile.facts`: plain-language answers to the screening questions that show up everywhere: work authorization, sponsorship, availability dates, legal name, how you heard about the role, number of internships, and so on. **This matters most.** Anything not covered here or in your resume gets flagged for review instead of guessed, so most review flags can be fixed by adding a line here.
- `filters`: which postings to consider. `categories` and `terms` must match the list's wording exactly (on Simplify, software roles are `"Software"`, not `"Software Engineering"`).
- `eeo`: demographic answers, or `decline`.

`config.yaml`, your resume, `state.db`, screenshots and the generated reports are gitignored because they contain personal information.

## Use

```bash
.venv/bin/python autoapply.py try <application-url>   # fill one form in a visible browser; never submits
.venv/bin/python autoapply.py run                     # fetch lists and process new postings
.venv/bin/python autoapply.py review                  # approve/edit drafted answers, then submit
.venv/bin/python autoapply.py status                  # counts per status
.venv/bin/python autoapply.py install-schedule        # runs `run` daily at schedule_hours (launchd)
```

Suggested first steps:

1. `try` one Greenhouse or Ashby link and check every answer in the browser.
2. `run` with `dry_run: true` (the default). Forms with no flagged answers are filled and screenshotted to `screenshots/` without submitting. Held applications are **not** filled until you approve them in `review`.
3. Go through `review`, look at a few screenshots, and add lines to `profile.facts` for anything that keeps getting flagged.
4. Set `dry_run: false`. Postings filled during dry runs are submitted on the next real run. Watch the first few real submissions: the submit and confirmation-page step has had less testing than filling.
5. `install-schedule`.

## How it works

- `sources.py` reads each repo's `.github/scripts/listings.json` (or its README tables) and applies your `filters`. A role posted under several locations or links (same company and title, ignoring "Summer 2027"-style suffixes) is applied to once, keeping the location closest to you; the rest are recorded as `duplicate`.
- `forms.py` opens the real application page in Chrome and reads every field: label, type, options and whether it's required. Then it fills them, uploads your resume, submits, and waits for a confirmation page.
- `drafting.py` sends the fields, your profile, your resume and the job description to Claude. Each answer is tagged `factual` or `open_ended`, plus a confidence flag, and the response says whether the role is one you can't hold. An application is held for review if any answer is open-ended or low-confidence, or a required field is unanswered. If the role is ineligible, the next duplicate of it (e.g. another location) is queued instead.
- If filling hits an answer that doesn't match the form's options, the application goes to review instead of being submitted.
- `store.py` keeps every posting and its status in `state.db`: `queued`, `filtered`, `duplicate`, `manual`, `needs_review`, `approved`, `dry_run`, `applied`, `ineligible`, `blocked`, `failed`, `skipped`, `closed`.
- Real submissions are spaced 8–25 seconds apart; dry runs and held applications don't wait.

## Possible next steps

- **Speed:** draft the next posting's answers while the browser works on the current one (roughly 10–15 s saved per posting), and shorten the 15 s "wait for the page to settle" cap after testing it on a few forms.
- **More eligibility rules:** skip roles that require living in the US or a specific state (these currently land in review).
- **Re-draft after config changes:** a command to re-queue held applications so they pick up new `facts` (today this is done by hand in `state.db`).
- **Fill held applications in dry runs** too, so you can see the layout of every form before approving.
- **More job sites:** Workday is the biggest gap. SmartRecruiters and Workable have consistent forms and would be the easiest to add next.
- **Duplicates across lists:** if you watch several repos, the same job may come with different titles; matching on the job's ID would catch more of these.
- **Tests:** there are none yet. Saved HTML of a few real forms would let `forms.scan` / `forms.fill` be tested without a browser.
