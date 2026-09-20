#!/usr/bin/env python3
"""Poll the Wroclaw pasport.org.ua e-queue for free appointment days.

The site sits behind Cloudflare, so requests go out through curl_cffi with a
Chrome TLS fingerprint. The page embeds a per-session CSRF token that the
booking endpoint requires, so every run is: GET page -> read token -> POST.
"""

import json
import os
import random
import re
import sys
import time
import uuid

from curl_cffi import requests

PAGE = "https://wroclaw.pasport.org.ua/solutions/e-queue"
ORIGIN = "https://wroclaw.pasport.org.ua"
SERVICE_ID = os.environ.get("SERVICE_ID", "4")
CENTER_ID = os.environ.get("CENTER_ID", "13")
BOUNDARY = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]

CSRF_RE = re.compile(r"&quot;csrf&quot;:&quot;([0-9a-f]{32})&quot;")


def multipart(fields):
    body = "".join(
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        for k, v in fields.items()
    )
    return (body + f"--{BOUNDARY}--\r\n").encode()


def new_session():
    s = requests.Session(impersonate="chrome")
    # The site's JS stores a ThumbmarkJS browser fingerprint here; the server
    # only wants the cookie to be present and stable-looking.
    s.cookies.set("dpuniq", uuid.uuid4().hex, domain="wroclaw.pasport.org.ua")
    return s


def post(session, csrf, fields):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "Referer": PAGE,
        "Origin": ORIGIN,
    }
    payload = dict(fields)
    payload[csrf] = "1"
    return session.post(PAGE, headers=headers, data=multipart(payload), timeout=45)


def fetch_days():
    """Return the list of bookable days, or raise on failure."""
    session = new_session()

    page = session.get(PAGE, timeout=45)
    if page.status_code != 200:
        raise RuntimeError(f"page returned HTTP {page.status_code}")

    match = CSRF_RE.search(page.text)
    if not match:
        raise RuntimeError("CSRF token not found in page (layout changed?)")
    csrf = match.group(1)

    # Mirror the browser: the widget asks whether the service has days at all
    # before it asks for the list.
    post(session, csrf, {"form": "check_services",
                         "ServiceCenterId": CENTER_ID,
                         "ServiceId": SERVICE_ID})
    time.sleep(1.5)

    resp = post(session, csrf, {"form": "days",
                                "ServiceCenterId": CENTER_ID,
                                "ServiceId": SERVICE_ID})
    if resp.status_code != 200:
        raise RuntimeError(f"days endpoint returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"non-JSON reply: {resp.text[:200]!r}")

    if not isinstance(data, dict) or "days" not in data:
        raise RuntimeError(f"unexpected reply: {json.dumps(data)[:200]}")

    return [d for d in data["days"] if d.get("isAllowed") is True]


def fetch_days_with_retry(attempts=3):
    last = None
    for i in range(attempts):
        try:
            return fetch_days()
        except Exception as exc:  # noqa: BLE001 - reported to the job log
            last = exc
            print(f"attempt {i + 1}/{attempts} failed: {exc}", file=sys.stderr)
            if i < attempts - 1:
                time.sleep(20 * (i + 1) + random.uniform(0, 10))
    raise last


def notify(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("::warning::TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set, skipping notification")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"::error::Telegram rejected the message: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    print("notification sent")


def summary(line):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line)


def main():
    state_file = os.environ.get("STATE_FILE", "state/last_seen.json")
    try:
        previous = set(json.load(open(state_file, encoding="utf-8")))
    except Exception:
        previous = set()

    try:
        days = fetch_days_with_retry()
    except Exception as exc:  # noqa: BLE001
        # Transient Cloudflare blocks and 429s are normal here. Warn loudly but
        # do not fail the run, so a flaky hour does not bury the job log in red.
        summary(f"⚠️ check failed: {exc}")
        print(f"::warning::check failed: {exc}")
        return 0

    dates = sorted(d["date"] for d in days)
    if not dates:
        summary("No free slots.")
        os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
        json.dump([], open(state_file, "w", encoding="utf-8"))
        return 0

    fresh = [d for d in dates if d not in previous]
    summary(f"Free days: {', '.join(dates)}" + ("" if fresh else " (already notified)"))

    if fresh:
        lines = ["🛂 <b>Є вільні слоти!</b> Закордонний паспорт, Вроцлав", ""]
        for day in days:
            if day["date"] in fresh:
                count = day.get("allowedJobCount", "?")
                lines.append(f"• <b>{day.get('datePart', day['date'])}</b> — місць: {count}")
        lines += ["", f'<a href="{PAGE}">Записатись →</a>']
        notify("\n".join(lines))

    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    json.dump(dates, open(state_file, "w", encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
