"""Train Ticket Availability & Seat Tracker Bot for Indian Railways.

Monitors 4 major travel corridors between Palani/Dindigul and Chennai:
  1. Palani (PLNI) ➔ Chennai Central (MAS)
  2. Chennai Central (MAS) ➔ Palani (PLNI)
  3. Dindigul (DG) ➔ Chennai (MS/MAS/TBM)
  4. Chennai (MS/MAS/TBM) ➔ Dindigul (DG)

Scans up to 60 days in advance (configurable via SCAN_DAYS).
Sends Telegram alerts when:
  - Fresh booking opens or seats become AVAILABLE
  - A previously sold-out/waitlisted train reopens with confirmed seats
  - Extra coaches/quota are added (seat count surges)
Zero duplicate alerts for unchanged availability.
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- Configurations ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

UTC = timezone.utc

STATE_FILE       = "state.json"
DIVIDER          = "━━━━━━━━━━━━━━━━"
FAIL_ALERT_AFTER = 3
SCAN_DAYS        = int(os.environ.get("SCAN_DAYS", "60"))
API_DELAY        = float(os.environ.get("API_DELAY", "0.12"))  # Fast & reliable delay between calls

# 4 Core corridors (ConfirmTkt automatically clusters MS/MAS/TBM under Chennai)
ROUTES = [
    {"from": "PLNI", "to": "MAS", "label": "Palani ➔ Chennai Central"},
    {"from": "MAS", "to": "PLNI", "label": "Chennai Central ➔ Palani"},
    {"from": "DG",   "to": "MS",  "label": "Dindigul ➔ Chennai"},
    {"from": "MS",   "to": "DG",  "label": "Chennai ➔ Dindigul"},
]

# Preferred class order
CLASS_ORDER = ["1A", "2A", "3A", "3E", "CC", "EC", "SL", "2S"]

API_URL = "https://cttrainsapi.confirmtkt.com/api/v1/trains/search"


def get_browser_headers() -> dict:
    """Generate realistic browser headers with randomized device ID per session."""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9,ta;q=0.8",
        "Origin": "https://www.confirmtkt.com",
        "Referer": "https://www.confirmtkt.com/",
        "clientid": "ct-web",
        "apikey": "ct-web!2$",
        "deviceid": str(uuid.uuid4()),
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


# --- Date & Description Helpers ---
def get_relative_day_desc(target_date: datetime, now: datetime) -> str:
    """Returns friendly calendar description (e.g. 'NEXT WEEK SATURDAY', 'TOMORROW')."""
    diff_days = (target_date.date() - now.date()).days
    weekday_name = target_date.strftime("%A").upper()

    if diff_days == 0:
        desc = "TODAY"
    elif diff_days == 1:
        desc = "TOMORROW"
    elif diff_days == 2:
        desc = "DAY AFTER TOMORROW"
    elif 3 <= diff_days <= 6:
        desc = f"THIS {weekday_name}"
    elif 7 <= diff_days <= 13:
        desc = f"NEXT WEEK {weekday_name}"
    else:
        desc = f"IN {diff_days} DAYS ({weekday_name})"

    # Tag weekend (Friday, Saturday, Sunday)
    is_weekend = target_date.weekday() in (4, 5, 6)
    weekend_tag = "[🚨 WEEKEND]" if is_weekend else "[WEEKDAY]"
    return f"{desc} {weekend_tag}"


def format_journey_date(dt: datetime) -> str:
    """Format: 'Sat 31 Oct 2026'"""
    return dt.strftime("%a %d %b %Y")


def format_duration(mins) -> str:
    try:
        m = int(mins)
        return f"{m // 60}h {m % 60}m"
    except (TypeError, ValueError):
        return f"{mins} mins"


# --- State Management ---
def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            logging.error(f"State read failed, starting fresh: {e}")
    if not isinstance(state, dict):
        state = {}
    state.setdefault("alerts", {})
    state.setdefault("fail_streak", 0)
    return state


def save_state(state, today_iso):
    """Save state atomically and prune past dates."""
    state["alerts"] = {
        k: v for k, v in state["alerts"].items()
        if isinstance(v, dict) and v.get("date_code", "") >= today_iso
    }
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp_file, STATE_FILE)


# --- Telegram Dispatcher ---
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials not set, printing alert to console:\n" + text)
        return True

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("ok"):
                return True
            if resp.status_code == 429:
                retry_sec = min(int(body.get("parameters", {}).get("retry_after", 5)), 30)
                time.sleep(retry_sec)
                continue
            logging.warning(f"Telegram attempt {attempt}: HTTP {resp.status_code} {body.get('description', '')}")
            if 400 <= resp.status_code < 500:
                # If Telegram Markdown parse error, retry once as plain text
                if "can't parse entities" in body.get("description", "").lower():
                    plain_payload = dict(payload)
                    plain_payload.pop("parse_mode", None)
                    requests.post(url, json=plain_payload, timeout=10)
                    return True
                return False
        except requests.RequestException as e:
            logging.warning(f"Telegram attempt {attempt}: {type(e).__name__}")
        time.sleep(2 * attempt)
    return False


# --- ConfirmTkt Fetcher ---
def fetch_trains(session, src: str, dst: str, date_str: str):
    """date_str must be DD-MM-YYYY"""
    params = {
        "sourceStationCode": src,
        "destinationStationCode": dst,
        "dateOfJourney": date_str,
    }
    try:
        resp = session.get(API_URL, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data and data.get("data"):
                return data["data"].get("trainList", [])
        elif resp.status_code == 400:
            # Date beyond advance reservation window or date in past
            return []
        else:
            logging.warning(f"ConfirmTkt {src}->{dst} on {date_str}: HTTP {resp.status_code}")
    except Exception as e:
        logging.warning(f"ConfirmTkt {src}->{dst} on {date_str} failed: {type(e).__name__}: {e}")
    return None


# --- Availability Parsing & Evaluation ---
def parse_seat_info(raw_info: dict):
    """Extract seats, availability status, and fare from a class cache object."""
    if not raw_info or not isinstance(raw_info, dict):
        return None

    data_source = raw_info.get("availabilityDataSource") or "NA"
    raw_avail = raw_info.get("availability")
    display_name = raw_info.get("availabilityDisplayName") or ""
    fare = raw_info.get("fare") or "0"
    prediction = raw_info.get("predictionPercentage", 0)

    # Distinguish between data not yet loaded vs real availability data
    has_real_data = data_source != "NA" and (raw_avail is not None or bool(display_name.strip()))

    raw_str = raw_avail or ""
    disp_str = display_name or ""

    # Detect if seats are Available (confirmed)
    m_avail = re.search(r'(?:AVAILABLE|AVL|CURR_AVL)[-\s]*0*(\d+)', raw_str, re.IGNORECASE)
    if not m_avail:
        m_avail = re.search(r'(?:AVAILABLE|AVL|CURR_AVL)[-\s]*0*(\d+)', disp_str, re.IGNORECASE)

    seats = int(m_avail.group(1)) if m_avail else None
    is_available = seats is not None and seats > 0

    # Also detect if RAC
    m_rac = re.search(r'RAC[-\s]*0*(\d+)', raw_str, re.IGNORECASE) or re.search(r'RAC[-\s]*0*(\d+)', disp_str, re.IGNORECASE)
    rac_seats = int(m_rac.group(1)) if m_rac else None

    return {
        "has_real_data": has_real_data,
        "is_available": is_available,
        "seats": seats if is_available else 0,
        "is_rac": rac_seats is not None,
        "rac_count": rac_seats or 0,
        "display_name": disp_str or raw_str,
        "fare": str(fare),
        "prediction": prediction,
    }


def scan_all_corridors(session, state: dict, now: datetime):
    """
    Scans from today to today + SCAN_DAYS across all routes.
    Eliminates cross-route duplicates using seen_keys.
    Returns (hits, ok_count, fail_count).
    """
    hits = []
    seen_keys = set()  # Tracks (train_no, date_code, cls_name) to avoid cross-route duplicates
    ok_count = 0
    fail_count = 0

    for day_offset in range(SCAN_DAYS):
        target_dt = now + timedelta(days=day_offset)
        date_api = target_dt.strftime("%d-%m-%Y")     # DD-MM-YYYY for API
        date_code = target_dt.strftime("%Y%m%d")      # YYYYMMDD for state keys
        rel_desc = get_relative_day_desc(target_dt, now)
        date_formatted = format_journey_date(target_dt)

        for route in ROUTES:
            src = route["from"]
            dst = route["to"]
            route_label = route["label"]

            train_list = fetch_trains(session, src, dst, date_api)
            time.sleep(API_DELAY)

            if train_list is None:
                fail_count += 1
                continue
            ok_count += 1

            for train in train_list:
                train_no = str(train.get("trainNumber", "")).strip()
                # Clean train name of underscores to ensure clean Markdown rendering
                train_name = train.get("trainName", "").strip().replace("_", " ")
                dep_time = train.get("departureTime", "")
                arr_time = train.get("arrivalTime", "")
                duration_m = train.get("duration", 0)

                cache = train.get("availabilityCache", {}) or {}

                available_classes = []
                for cls_name in CLASS_ORDER:
                    if cls_name not in cache:
                        continue
                    info = cache[cls_name]
                    parsed = parse_seat_info(info)
                    if not parsed:
                        continue

                    # Cross-route deduplication: don't alert the same train twice in one cycle
                    cross_route_key = (train_no, date_code, cls_name)
                    if cross_route_key in seen_keys:
                        continue

                    key = f"{train_no}_{date_code}_{cls_name}"
                    prev = state["alerts"].get(key)

                    if parsed["is_available"]:
                        seats = parsed["seats"]
                        kind = None

                        if prev is None:
                            kind = "new"  # Fresh booking open or first time detected
                        elif prev.get("sold_out"):
                            kind = "reopened"  # Was full/waitlisted, reopened!
                        elif seats >= prev.get("seats", 0) + 10:
                            kind = "surge"  # Noticeable quota increase or extra coach

                        if kind:
                            seen_keys.add(cross_route_key)
                            available_classes.append({
                                "class": cls_name,
                                "seats": seats,
                                "status": parsed["display_name"],
                                "fare": parsed["fare"],
                                "prediction": parsed["prediction"],
                                "kind": kind,
                                "key": key,
                            })
                    elif parsed["has_real_data"]:
                        # Real data confirms it's WL or REGRET (not just unqueried/NA)
                        seen_keys.add(cross_route_key)
                        if prev and not prev.get("sold_out"):
                            prev["sold_out"] = True
                            prev["seats"] = 0

                if available_classes:
                    hit = {
                        "train_no": train_no,
                        "train_name": train_name,
                        "src": src,
                        "dst": dst,
                        "route_label": route_label,
                        "date_api": date_api,
                        "date_code": date_code,
                        "date_formatted": date_formatted,
                        "rel_desc": rel_desc,
                        "dep_time": dep_time,
                        "arr_time": arr_time,
                        "duration": format_duration(duration_m),
                        "classes": available_classes,
                    }
                    hits.append(hit)

    return hits, ok_count, fail_count


# --- Formatting & Dispatching Alerts ---
def format_alert_card(hit: dict) -> str:
    lines = [
        f"🚆 *{hit['train_no']} - {hit['train_name']}*",
        f"⏰ Dep: {hit['dep_time']} ➔ Arr: {hit['arr_time']} ({hit['duration']})",
        "💺 *Available Seats:*",
    ]
    for c in hit["classes"]:
        tag = "🆕 BOOKING OPEN" if c["kind"] == "new" else ("🔁 RE-OPENED" if c["kind"] == "reopened" else "➕ SEATS ADDED")
        fare_str = f"₹{c['fare']}" if c['fare'] and c['fare'] != '0' else "Standard"
        lines.append(f"  • *{c['class']}*: ✅ *{c['seats']} Seats* | {fare_str} [{tag}]")

    ct_url = f"https://www.confirmtkt.com/rbooking/trains/from/{hit['src']}/to/{hit['dst']}/date/{hit['date_api']}"
    lines.append(f"🔗 [1-Click Book on ConfirmTkt]({ct_url}) · [IRCTC](https://www.irctc.co.in/nget/train-search)")
    return "\n".join(lines)


def send_alerts(hits: list, state: dict, now: datetime) -> int:
    """Group hits by Route & Date to avoid Telegram spam, and send chunked messages."""
    groups = {}
    for h in hits:
        group_key = (h["route_label"], h["date_code"], h["date_formatted"], h["rel_desc"])
        groups.setdefault(group_key, []).append(h)

    delivered = 0
    for (route_label, date_code, date_formatted, rel_desc), train_hits in groups.items():
        cards = [format_alert_card(th) for th in train_hits]

        header = (
            f"🚨 *TRAIN SEAT ALERT: SEATS AVAILABLE!* 🚨\n"
            f"📍 *Route:* {route_label}\n"
            f"📅 *Date:* {date_formatted} ({rel_desc})\n"
            f"{DIVIDER}"
        )

        full_message = header + "\n" + f"\n{DIVIDER}\n".join(cards)

        # Telegram character safe limit chunking
        if len(full_message) <= 3600:
            chunks = [full_message]
        else:
            chunks = []
            curr = header
            for card in cards:
                if len(curr) + len(card) + len(DIVIDER) + 4 > 3600:
                    chunks.append(curr)
                    curr = header + "\n" + card
                else:
                    curr += f"\n{DIVIDER}\n" + card
            if curr:
                chunks.append(curr)

        for chunk in chunks:
            if send_telegram(chunk):
                delivered += 1
                for th in train_hits:
                    for c in th["classes"]:
                        state["alerts"][c["key"]] = {
                            "train_no": th["train_no"],
                            "train_name": th["train_name"],
                            "date_code": date_code,
                            "class": c["class"],
                            "seats": c["seats"],
                            "fare": c["fare"],
                            "sold_out": False,
                            "alerted_at": now.isoformat(timespec="seconds"),
                            "kind": c["kind"],
                        }
            time.sleep(1)

    return delivered


# --- Health Monitor ---
def check_health(state: dict, ok_count: int, fail_count: int):
    if ok_count == 0 and fail_count > 0:
        state["fail_streak"] = min(state["fail_streak"] + 1, FAIL_ALERT_AFTER)
        if state["fail_streak"] == FAIL_ALERT_AFTER and not state.get("outage_alerted"):
            state["outage_alerted"] = True
            send_telegram(
                f"⚠️ *Train Bot Warning*: ConfirmTkt API unreachable for {FAIL_ALERT_AFTER} check cycles. "
                f"Alerts paused until server recovers."
            )
        return False
    if state.pop("outage_alerted", False):
        send_telegram("✅ *Train Bot Restored*: Connection to train enquiry server restored. Alerts active.")
    state["fail_streak"] = 0
    return True


# --- Main Runner ---
def main():
    now = datetime.now(IST)
    logging.info(f"Starting Train Ticket Availability Check (Scanning next {SCAN_DAYS} days)...")
    state = load_state()

    session = requests.Session()
    session.headers.update(get_browser_headers())
    try:
        hits, ok_count, fail_count = scan_all_corridors(session, state, now)
    finally:
        session.close()

    logging.info(f"Scan complete. API Queries: {ok_count} ok, {fail_count} failed. Hits: {len(hits)}")

    if hits:
        sent = send_alerts(hits, state, now)
        logging.info(f"Delivered {sent} grouped alert message(s) to Telegram.")
    else:
        logging.info("No new seat availability changes found.")

    healthy = check_health(state, ok_count, fail_count)
    save_state(state, now.strftime("%Y%m%d"))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
