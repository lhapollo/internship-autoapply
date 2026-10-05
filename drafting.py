"""Use Claude to answer application questions from your profile + resume.

Backends:
  - "api": Anthropic SDK (needs ANTHROPIC_API_KEY or `ant auth login`).
  - "cli": the `claude` CLI in print mode, which uses your Claude Code login.
  - "auto" (default): api if ANTHROPIC_API_KEY is set, else cli.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import yaml

MODEL = "claude-opus-5-5"

SYSTEM = """You fill out internship job applications on behalf of a candidate. You get the candidate's \
profile, their resume, the job posting, and a list of form fields. Answer every field you have \
information for.

Rules:
- Never invent facts. Use only the profile, the profile's `facts`, and the resume. If a factual question \
can't be answered from those, give your best answer and set "confident": false so a human checks it.
- For select / radio / combobox / yesno fields with listed options, "value" must be copied exactly from \
the options. For checkbox_group, put every applicable option (copied exactly) in "values". For a single \
checkbox, "value" is "checked" or "unchecked"; check required acknowledgement/consent boxes.
- Combobox fields marked searchable accept free text that will be typed into a search box \
(e.g. a school or city name); give the canonical name.
- Demographic / EEO questions (gender, race, ethnicity, veteran, disability, pronouns): use the \
profile's `eeo` preferences; where it says "decline" pick the decline / prefer-not-to-say option.
- Respect format hints in a field's placeholder. Full dates with no hint: MM/DD/YYYY (use the 15th when only \
the month is known). Year-only or month-only fields get just that part.
- Leave "value" empty for optional fields you have no information for (e.g. "how did you hear about us" \
can be "Online job board" though).
- kind = "open_ended" for questions that ask for a written, personalised response (why this company, \
describe a time when, what excites you, cover letter text, anything needing more than a short factual \
phrase). Write these in the first person, specific to the company and role and grounded in real \
resume details, 60-150 words unless the question asks for a length. Plain and sincere, no clichés.
- kind = "factual" for everything else (contact details, eligibility, yes/no, dates, links, short facts).
- "note": one short sentence for the human reviewer when confident is false or the answer needs a check; \
otherwise empty.
- "ineligible_reason": if the posting or a required form question makes clear the candidate can't hold \
this role, give a one-sentence reason. That covers: only students in (or holding) a Master's, MBA or PhD \
program (the candidate is an undergraduate); only high school students; only U.S. citizens, permanent \
residents or "U.S. persons" (e.g. ITAR / export-control or security-clearance roles), when the profile \
says the candidate isn't one. Not needing a visa sponsor or being willing to relocate doesn't make \
someone ineligible. Leave it empty when the candidate qualifies or it's unclear."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "values": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": ["factual", "open_ended"]},
                    "confident": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["key", "value", "values", "kind", "confident", "note"],
                "additionalProperties": False,
            },
        },
        "ineligible_reason": {"type": "string"},
    },
    "required": ["answers", "ineligible_reason"],
    "additionalProperties": False,
}


def _field_view(fields):
    out = []
    for f in fields:
        if f["kind"] == "file":
            continue
        v = {"key": f["key"], "kind": f["kind"], "label": f["label"][:400], "required": f["required"]}
        if f.get("options"):
            v["options"] = f["options"]
        if f.get("searchable"):
            v["searchable"] = True
        if f.get("placeholder"):
            v["placeholder"] = f["placeholder"]
        out.append(v)
    return out


def build_prompt(profile: dict, resume_text: str, job: dict, fields: list[dict]) -> str:
    return (
        f"<profile>\n{yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)}</profile>\n\n"
        f"<resume>\n{resume_text}\n</resume>\n\n"
        f"<job company={json.dumps(job['company'])} title={json.dumps(job['title'])}>\n"
        f"{job.get('description', '')}\n</job>\n\n"
        f"<fields>\n{json.dumps(_field_view(fields), indent=1, ensure_ascii=False)}\n</fields>\n\n"
        "Return one entry per field key."
    )


def _call_api(prompt: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        # Server-side fallback: if the request is declined, retry on a fallback model in the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to draft answers for this application")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _call_cli(prompt: str) -> dict:
    exe = os.environ.get("CLAUDE_CLI") or shutil.which("claude") or "/opt/homebrew/bin/claude"
    full = (f"{SYSTEM}\n\nRespond with ONLY a JSON object matching this JSON Schema, no prose, no code fences:\n"
            f"{json.dumps(SCHEMA)}\n\n{prompt}")
    with tempfile.TemporaryDirectory() as tmp:  # run outside any project so no CLAUDE.md leaks in
        r = subprocess.run([exe, "-p", "--output-format", "json", "--model", MODEL],
                           input=full, capture_output=True, text=True, timeout=600, cwd=tmp)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {(r.stderr or r.stdout)[:500]}")
    result = json.loads(r.stdout).get("result", "")
    m = re.search(r"\{.*\}", result, re.S)
    if not m:
        raise RuntimeError(f"claude CLI returned no JSON: {result[:300]}")
    return json.loads(m.group(0))


def draft_answers(profile, resume_text, job, fields, backend="auto") -> tuple[dict, str]:
    """Return ({field_key: {"value", "values", "kind", "confident", "note"}}, ineligible_reason)."""
    if backend == "auto":
        backend = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
    prompt = build_prompt(profile, resume_text, job, fields)
    data = _call_api(prompt) if backend == "api" else _call_cli(prompt)
    return {a["key"]: a for a in data.get("answers", [])}, (data.get("ineligible_reason") or "").strip()


def needs_review(fields: list[dict], answers: dict) -> list[str]:
    """Field keys a human must look at before submitting."""
    flagged = []
    for f in fields:
        if f["kind"] == "file":
            continue
        a = answers.get(f["key"])
        empty = not a or (not (a.get("value") or "").strip() and not a.get("values"))
        if empty:
            if f["required"]:
                flagged.append(f["key"])  # optional fields can just stay blank
        elif a["kind"] == "open_ended" or not a["confident"]:
            flagged.append(f["key"])
    return flagged
