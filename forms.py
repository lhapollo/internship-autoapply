"""Generic, DOM-driven application-form handling for Greenhouse, Lever and Ashby.

The ATS APIs don't describe the whole form (e.g. Greenhouse's live page adds
education/employment sections the API omits), so we read the rendered page:
every visible control is grouped into a "field" with a label, kind, required
flag and options, and tagged with a `data-aa` attribute so we can fill it later.
"""
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import Error as PWError, Page

SCAN_JS = r"""
() => {
  const clean = t => (t || '').replace(/[\s ]+/g, ' ').trim();
  const visible = e => {
    if (!e) return false;
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || e.type === 'file' || e.type === 'radio' || e.type === 'checkbox';
  };
  const textWithoutControls = el => {
    const c = el.cloneNode(true);
    c.querySelectorAll('input,select,textarea,[role=listbox],[role=option],button').forEach(n => n.remove());
    return clean(c.innerText || c.textContent);
  };
  const QSEL = 'label, legend, .application-label, [class*="label"], [class*="Label"], [class*="question"], [class*="title"], h3, h4, p, span';
  // Closest preceding label-ish text that isn't itself wrapping a control.
  const nearLabel = (el, exclude) => {
    let a = el;
    for (let d = 0; d < 6 && a && a !== document.body; d++) {
      a = a.parentElement; if (!a) break;
      const cands = [...a.querySelectorAll(QSEL)].filter(c =>
        !c.contains(el) && !c.querySelector('input,select,textarea') &&
        (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) &&
        !(exclude || []).some(x => c.contains(x) || x.contains(c)) &&
        clean(c.innerText).length >= 2 && clean(c.innerText).length < 600);
      if (cands.length) return clean(cands[cands.length - 1].innerText);
    }
    return '';
  };
  const ownLabel = el => {
    if (el.labels && el.labels.length) { const t = textWithoutControls(el.labels[0]); if (t) return t; }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = clean(lb.split(' ').map(id => document.getElementById(id)?.innerText).join(' ')); if (t) return t; }
    return clean(el.getAttribute('aria-label') || '');
  };
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$|[*✱]\s/.test(label);
  const form = document.querySelector('form') || document.body;
  const fields = []; let n = 0;
  const tag = (el, key, opt) => { el.setAttribute('data-aa', key); if (opt !== undefined) el.setAttribute('data-aa-opt', opt); };
  const seenGroups = new Set();

  for (const el of form.querySelectorAll('input, textarea, select')) {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (!visible(el) && type !== 'file') continue;
    if (/requiredInput/.test(el.className) || el.closest('[aria-hidden=true]')) continue;
    if (el.closest('[class*=captcha], iframe')) continue;

    if (type === 'radio' || type === 'checkbox') {
      const container = el.closest('fieldset, [role=radiogroup], [role=group]');
      let members;
      if (type === 'radio' && el.name) members = [...form.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)];
      else if (container) members = [...container.querySelectorAll(`input[type=${type}]`)];
      else members = [el];
      if (members.length > 1 || type === 'radio') {
        const gid = members[0];
        if (seenGroups.has(gid)) continue;
        seenGroups.add(gid);
        const key = 'f' + (n++);
        const legend = container?.querySelector('legend');
        let label = legend ? textWithoutControls(legend) : '';
        if (!label) label = nearLabel(members[0], members.map(m => m.labels?.[0]).filter(Boolean));
        const options = members.map((m, i) => { tag(m, key, i); return (ownLabel(m) || m.value || '').slice(0, 160); });
        fields.push({key, kind: type === 'radio' ? 'radio' : 'checkbox_group', label, options,
                     required: isRequired(members[0], label) || /[*✱]/.test(label)});
        continue;
      }
      const key = 'f' + (n++); tag(el, key);
      const label = ownLabel(el) || nearLabel(el);
      fields.push({key, kind: 'checkbox', label, required: isRequired(el, label), options: ['checked', 'unchecked']});
      continue;
    }

    const key = 'f' + (n++); tag(el, key);
    let label = ownLabel(el);
    if (!label || /^(attach|upload|select\.{0,3})$/i.test(label)) label = nearLabel(el) || label;
    const f = {key, label, required: isRequired(el, label), id: el.id || '', name: el.name || '',
               placeholder: el.placeholder || '', input_type: type};
    if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^select\b|^-+$|^choose/i.test(t));
    } else if (type === 'file') {
      f.kind = 'file';
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) {
      f.kind = 'combobox';
    } else {
      f.kind = el.tagName === 'TEXTAREA' ? 'textarea' : 'text';
    }
    fields.push(f);
  }

  // Ashby-style Yes/No button pairs.
  for (const box of form.querySelectorAll('div, fieldset')) {
    const btns = [...box.children].filter(c => c.tagName === 'BUTTON');
    if (btns.length === 2 && btns.every(b => /^(yes|no)$/i.test(clean(b.innerText)))) {
      const key = 'f' + (n++);
      btns.forEach((b, i) => tag(b, key, i));
      const label = nearLabel(box);
      fields.push({key, kind: 'yesno', label, options: btns.map(b => clean(b.innerText)), required: /[*✱]/.test(label)});
    }
  }
  return fields;
}
"""

SUCCESS_TEXT = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(submitted|received)"
    r"|successfully (submitted|applied)|we('|’)ve received your application", re.I)
VERIFY_TEXT = re.compile(r"(security|verification) code|verify your email|check your (email|inbox)", re.I)


def _best(options: list[str], answer: str) -> int | None:
    """Index of the option that best matches `answer`."""
    if not options or not answer:
        return None
    a = answer.strip().lower()
    for i, o in enumerate(options):
        if o.strip().lower() == a:
            return i
    for i, o in enumerate(options):
        if o.strip().lower().startswith(a) or a.startswith(o.strip().lower()):
            return i
    scores = [SequenceMatcher(None, a, o.lower()).ratio() for o in options]
    i = max(range(len(options)), key=scores.__getitem__)
    return i if scores[i] >= 0.5 else None


def open_form(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWError:
        pass
    # Some boards show the description first with an "Apply" button.
    if not page.locator("input[type=file], textarea, input[type=email]").count():
        btn = page.get_by_role("button", name=re.compile(r"^apply", re.I))
        link = page.get_by_role("link", name=re.compile(r"^apply", re.I))
        for loc in (btn, link):
            if loc.count():
                loc.first.click()
                page.wait_for_timeout(2500)
                break


def page_text(page: Page, limit=15000) -> str:
    return re.sub(r"\n{3,}", "\n\n", page.inner_text("body"))[:limit]


def scan(page: Page) -> list[dict]:
    fields = page.evaluate(SCAN_JS)
    for f in fields:
        if f["kind"] == "combobox":
            f["options"], f["searchable"] = _combobox_options(page, f["key"])
    return fields


def _combobox_options(page: Page, key: str) -> tuple[list[str], bool]:
    el = page.locator(f'[data-aa="{key}"]')
    try:
        el.click(timeout=3000)
        page.wait_for_timeout(500)
        opts = page.locator("[role=option]:visible").all_inner_texts()
        el.press("Escape")
    except PWError:
        return [], True
    opts = [o.strip() for o in opts if o.strip() and not re.match(r"no options|type to search|loading", o, re.I)]
    # Long or empty lists are type-ahead searches (schools, cities, ...).
    return (opts if len(opts) <= 80 else opts[:20]), (not opts or len(opts) > 80)


def fill(page: Page, fields: list[dict], answers: dict, resume: Path, cover_letter: Path | None) -> list[str]:
    """Fill the form. Returns human-readable problems (empty list = clean fill)."""
    problems = []
    # Typed fields first: some widgets (e.g. Greenhouse's phone input) reset a dropdown when typed into.
    ordered = sorted(fields, key=lambda f: f["kind"] not in ("file", "text", "textarea"))
    for f in ordered:
        key, kind, label = f["key"], f["kind"], f["label"][:70]
        loc = page.locator(f'[data-aa="{key}"]')
        try:
            if kind == "file":
                tag = f"{label} {f.get('id')} {f.get('name')}".lower()
                if re.search(r"resume|cv\b", tag):
                    loc.set_input_files(str(resume))
                    # Lever/Ashby parse the resume and overwrite fields; let that finish before typing.
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PWError:
                        pass
                    page.wait_for_timeout(2000)
                elif re.search(r"cover", tag) and cover_letter:
                    loc.set_input_files(str(cover_letter))
                elif f["required"] and not re.search(r"cover", tag):
                    problems.append(f"required upload I don't know how to fill: {label!r}")
                elif f["required"]:
                    problems.append(f"required cover letter upload: {label!r}")
                page.wait_for_timeout(1500)
                continue

            a = answers.get(key) or {}
            value, values = (a.get("value") or "").strip(), a.get("values") or []
            if not value and not values:
                if f["required"]:
                    problems.append(f"no answer for required field {label!r}")
                continue

            if kind in ("text", "textarea"):
                if f.get("name") == "location":
                    _fill_lever_location(page, loc, value)
                else:
                    loc.fill(value)
            elif kind == "select":
                i = _best(f["options"], value)
                if i is None:
                    problems.append(f"{label!r}: {value!r} isn't one of the options")
                else:
                    loc.select_option(label=f["options"][i])
            elif kind == "combobox":
                if not _fill_combobox(page, loc, value, f.get("options") or []):
                    problems.append(f"{label!r}: couldn't select {value!r}")
            elif kind in ("radio", "yesno"):
                i = _best(f["options"], value)
                if i is None:
                    problems.append(f"{label!r}: {value!r} isn't one of the options")
                else:
                    opt = page.locator(f'[data-aa="{key}"][data-aa-opt="{i}"]')
                    opt.check(force=True) if kind == "radio" else opt.click()
            elif kind == "checkbox":
                if value.lower() in ("checked", "yes", "true"):
                    loc.check(force=True)
            elif kind == "checkbox_group":
                for v in values or [value]:
                    i = _best(f["options"], v)
                    if i is None:
                        problems.append(f"{label!r}: {v!r} isn't one of the options")
                    else:
                        page.locator(f'[data-aa="{key}"][data-aa-opt="{i}"]').check(force=True)
        except PWError as e:
            problems.append(f"{label!r}: {str(e).splitlines()[0][:120]}")
    return problems


def _fill_lever_location(page: Page, loc, value: str):
    """Lever's location box only accepts a pick from its suggestion list."""
    loc.fill("")
    loc.type(value.split(",")[0], delay=50)
    page.wait_for_timeout(2500)
    sugg = page.locator(".dropdown-location:visible")
    if sugg.count():
        texts = sugg.all_inner_texts()
        sugg.nth(_best(texts, value) or 0).click()
    else:
        loc.fill(value)


def _fill_combobox(page: Page, loc, value: str, options: list[str]) -> bool:
    loc.click()
    loc.fill("")
    loc.type(value[:40], delay=30)
    page.wait_for_timeout(1200)
    opts = page.locator("[role=option]:visible")
    texts = opts.all_inner_texts()
    i = _best(texts, value)
    if i is None and len(texts) == 1 and not re.match(r"no options", texts[0], re.I):
        i = 0
    if i is None:
        loc.press("Escape")
        return False
    opts.nth(i).click()
    return True


def submit(page: Page, wait_for_human: int = 0) -> tuple[str, str]:
    """Click submit and classify the outcome as (status, detail)."""
    btn = page.get_by_role("button", name=re.compile(r"submit", re.I))
    if not btn.count():
        btn = page.locator("button[type=submit], input[type=submit]")
    if not btn.count():
        return "failed", "couldn't find a submit button"
    start_url = page.url
    btn.last.click()
    deadline = time.time() + 25 + wait_for_human
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        try:
            text = page.inner_text("body")
        except PWError:
            continue
        if page.url != start_url and re.search(r"confirm|thank|success|submitted", page.url, re.I):
            return "applied", f"confirmation page: {page.url}"
        if SUCCESS_TEXT.search(text):
            return "applied", "confirmation text detected"
        if VERIFY_TEXT.search(text) and not wait_for_human:
            return "blocked", "site asked for an email verification code"
        if _captcha_challenge(page) and not wait_for_human:
            return "blocked", "CAPTCHA challenge"
    errors = page.locator("[class*=error]:visible, [role=alert]:visible").all_inner_texts()
    errors = [e.strip() for e in errors if e.strip()][:5]
    if _captcha_challenge(page):
        return "blocked", "CAPTCHA challenge"
    return "failed", "no confirmation after submit" + (f"; errors: {errors}" if errors else "")


def _captcha_challenge(page: Page) -> bool:
    for fr in page.locator("iframe").all():
        src = (fr.get_attribute("src") or "")
        if re.search(r"hcaptcha.*(challenge|frame=challenge)|recaptcha/.*bframe", src) and fr.is_visible():
            box = fr.bounding_box()
            if box and box["height"] > 100:
                return True
    return False
