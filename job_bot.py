import os
import sys
import time
import logging
import sqlite3
import random
import requests
import json
from datetime import datetime
from typing import Optional
from http.cookiejar import Cookie, CookieJar
from dotenv import load_dotenv

load_dotenv()

# --- CONFIG from ENV ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOCATION = os.getenv("LOCATION", "Leigh, WN7")
JOB_QUERY = os.getenv("JOB_QUERY", "part time")
RADIUS_MILES = 5
JOBS_TO_SCRAPE = 33
JOBS_TO_SEND = 8
DB_PATH = "jobs_sent.db"
POLL_INTERVAL = 3  # seconds for Telegram polling

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set as environment variables.")
    sys.exit(1)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger()

# --- Load cookies from env var or cookie.json ---
def load_cookies():
    cookie_data = os.getenv("COOKIE_JSON")
    if cookie_data:
        log.info("Loading cookies from COOKIE_JSON environment variable.")
        try:
            cookies = json.loads(cookie_data)
            if not isinstance(cookies, list):
                raise ValueError("COOKIE_JSON must be a JSON array")
            return cookies
        except Exception as e:
            log.error(f"Failed to parse COOKIE_JSON env var: {e}")
            return None

    cookie_file = "cookie.json"
    if os.path.isfile(cookie_file):
        log.info(f"Loading cookies from {cookie_file} file.")
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if not isinstance(cookies, list):
                raise ValueError(f"{cookie_file} must contain a JSON array")
            return cookies
        except Exception as e:
            log.error(f"Failed to load cookies from {cookie_file}: {e}")
            return None

    log.error("No cookies found: Set COOKIE_JSON env var or provide cookie.json file.")
    return None

def add_cookies_to_session(session: requests.Session, cookies: list):
    jar = CookieJar()
    for c in cookies:
        try:
            cookie = Cookie(
                version=0,
                name=c["name"],
                value=c["value"],
                port=None,
                port_specified=False,
                domain=c["domain"],
                domain_specified=True,
                domain_initial_dot=c["domain"].startswith('.'),
                path=c.get("path", "/"),
                path_specified=True,
                secure=c.get("secure", False),
                expires=c.get("expirationDate"),
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            jar.set_cookie(cookie)
        except KeyError as e:
            log.warning(f"Skipping invalid cookie missing {e}: {c}")
    session.cookies = jar

# --- SQLite setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_jobs (
            job_id TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

# --- Indeed scraper ---
def scrape_indeed_jobs(query: str, location: str, radius: int, max_results: int, cookies: list):
    jobs = []
    base_url = "https://uk.indeed.com/jobs"
    params = {
        "q": query,
        "l": location,
        "radius": radius,
        "jt": "parttime",
        "limit": 50,
        "start": 0,
    }
    session = requests.Session()
    add_cookies_to_session(session, cookies)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://uk.indeed.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    while len(jobs) < max_results:
        params["start"] = len(jobs)
        try:
            r = session.get(base_url, params=params, headers=headers, timeout=10)
            r.raise_for_status()
        except Exception as e:
            log.error(f"Failed to fetch jobs page: {e}")
            break

        text = r.text
        job_ids = set()

        lines = text.splitlines()
        for line in lines:
            if 'data-jk="' in line:
                start_idx = line.find('data-jk="') + 9
                end_idx = line.find('"', start_idx)
                job_id = line[start_idx:end_idx]
                if job_id in job_ids:
                    continue
                job_ids.add(job_id)
                idx_title = line.find('title="')
                if idx_title != -1:
                    start_title = idx_title + 7
                    end_title = line.find('"', start_title)
                    title = line[start_title:end_title]
                else:
                    title = "Job"
                url = f"https://uk.indeed.com/viewjob?jk={job_id}"
                jobs.append({"id": job_id, "title": title.strip(), "url": url})
                if len(jobs) >= max_results:
                    break

        if len(job_ids) == 0:
            break

        time.sleep(random.uniform(1, 3))

    log.info(f"Scraped {len(jobs)} jobs from Indeed.")
    return jobs[:max_results]

# --- Telegram helpers ---
def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False

def get_updates(token: str, offset: Optional[int]) -> dict:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 20, "offset": offset}
    try:
        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Failed to get Telegram updates: {e}")
        return {}

# --- Job sending logic ---
def send_new_jobs(conn, token, chat_id, jobs, max_send):
    c = conn.cursor()
    new_jobs = []
    for job in jobs:
        c.execute("SELECT 1 FROM sent_jobs WHERE job_id=?", (job["id"],))
        if c.fetchone() is None:
            new_jobs.append(job)
        if len(new_jobs) >= max_send:
            break
    if not new_jobs:
        log.info("No new jobs to send.")
        return 0
    sent_count = 0
    for job in new_jobs:
        text = f"{job['title']}\n{job['url']}"
        if send_telegram_message(token, chat_id, text):
            c.execute("INSERT INTO sent_jobs (job_id, sent_at) VALUES (?, ?)", (job["id"], datetime.utcnow().isoformat()))
            conn.commit()
            sent_count += 1
            log.info(f"Sent job: {job['title']}")
        else:
            log.error(f"Failed to send job: {job['title']}")
    return sent_count

# --- /test command handler ---
def handle_test_command(conn, cookies):
    log.info("/test command received - sending 1 random unsent job")
    jobs = scrape_indeed_jobs(JOB_QUERY, LOCATION, RADIUS_MILES, JOBS_TO_SCRAPE, cookies)
    c = conn.cursor()
    unsent_jobs = []
    for job in jobs:
        c.execute("SELECT 1 FROM sent_jobs WHERE job_id=?", (job["id"],))
        if c.fetchone() is None:
            unsent_jobs.append(job)
    if not unsent_jobs:
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, "No new jobs available to send.")
        return
    job = random.choice(unsent_jobs)
    text = f"{job['title']}\n{job['url']}"
    if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, text):
        c.execute("INSERT INTO sent_jobs (job_id, sent_at) VALUES (?, ?)", (job["id"], datetime.utcnow().isoformat()))
        conn.commit()
        log.info(f"Sent job (test): {job['title']}")
    else:
        log.error("Failed to send job on /test command.")

def main():
    cookies = load_cookies()
    if not cookies:
        log.error("Aborting: No valid cookies loaded.")
        sys.exit(1)

    conn = init_db()

    offset = None

    jobs = scrape_indeed_jobs(JOB_QUERY, LOCATION, RADIUS_MILES, JOBS_TO_SCRAPE, cookies)
    sent = send_new_jobs(conn, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, jobs, JOBS_TO_SEND)
    log.info(f"Sent {sent} new jobs on startup.")

    log.info("Entering long polling loop for /test command...")

    while True:
        updates = get_updates(TELEGRAM_TOKEN, offset)
        if not updates or not updates.get("ok"):
            time.sleep(POLL_INTERVAL)
            continue
        results = updates.get("result", [])
        for update in results:
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            text = message.get("text", "")
            chat_id = message.get("chat", {}).get("id")
            if chat_id != int(TELEGRAM_CHAT_ID):
                continue
            if text.strip().lower() == "/test":
                handle_test_command(conn, cookies)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
