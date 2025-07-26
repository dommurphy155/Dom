#!/usr/bin/env python3
import os
import sys
import time
import logging
import sqlite3
import random
import json
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOCATION = os.getenv("LOCATION", "Leigh, WN7")
JOB_QUERY = os.getenv("JOB_QUERY", "part time")
RADIUS_MILES = 5
JOBS_TO_SCRAPE = 33
JOBS_TO_SEND = 8
DB_PATH = "jobs_sent.db"
POLL_INTERVAL = 3

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger()

def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sent_jobs (job_id TEXT PRIMARY KEY, sent_at TEXT NOT NULL)""")
    conn.commit()
    return conn

def load_playwright_cookies():
    raw = os.getenv("COOKIE_JSON", "")
    if not raw:
        log.error("COOKIE_JSON env var empty. Put cookie JSON array or path to file there.")
        return []
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError:
        if not os.path.isfile(raw):
            log.error("COOKIE_JSON is not valid JSON or file path.")
            return []
        with open(raw, "r") as f:
            cookies = json.load(f)
    sanitized = []
    for c in cookies:
        same_site = c.get("sameSite", "Lax")
        if same_site not in ["Strict", "Lax", "None"]:
            same_site = "Lax"
        sanitized.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": same_site,
            "expires": int(c.get("expirationDate", 0))
        })
    return sanitized

def scrape_indeed_jobs_pw(query, location, cookies, max_results):
    jobs = []
    url = f"https://uk.indeed.com/jobs?q={query}&l={location}&radius={RADIUS_MILES}&jt=parttime"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        page.goto(url, timeout=30000)
        page.wait_for_selector('a.tapItem', timeout=15000)
        els = page.query_selector_all('a.tapItem')
        for el in els:
            if len(jobs) >= max_results:
                break
            jk = el.get_attribute('data-jk')
            title_el = el.query_selector('h2.jobTitle span')
            title = title_el.inner_text().strip() if title_el else "Job"
            jobs.append({"id": jk, "title": title, "url": f"https://uk.indeed.com/viewjob?jk={jk}"})
        browser.close()
    log.info("Scraped %d jobs via Playwright", len(jobs))
    return jobs

def send_telegram_message(token, chat_id, text):
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram send error: %s", e)
        return False

def send_new_jobs(conn, jobs):
    c = conn.cursor()
    new = []
    for j in jobs:
        c.execute("SELECT 1 FROM sent_jobs WHERE job_id=?", (j["id"],))
        if not c.fetchone():
            new.append(j)
        if len(new) >= JOBS_TO_SEND:
            break
    if not new:
        log.info("No new jobs.")
        return 0
    count = 0
    for j in new:
        txt = f"{j['title']}\n{j['url']}"
        if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, txt):
            c.execute("INSERT INTO sent_jobs (job_id, sent_at) VALUES (?, ?)", (j["id"], datetime.utcnow().isoformat()))
            conn.commit()
            count += 1
    return count

def handle_test(conn, cookies):
    log.info("/test invoked")
    jobs = scrape_indeed_jobs_pw(JOB_QUERY, LOCATION, cookies, JOBS_TO_SCRAPE)
    c = conn.cursor()
    unsent = [j for j in jobs if not c.execute("SELECT 1 FROM sent_jobs WHERE job_id=?", (j["id"],)).fetchone()]
    if not unsent:
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, "No new jobs to send.")
        return
    job = random.choice(unsent)
    txt = f"{job['title']}\n{job['url']}"
    if send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, txt):
        c.execute("INSERT INTO sent_jobs (job_id, sent_at) VALUES (?, ?)", (job["id"], datetime.utcnow().isoformat()))
        conn.commit()
        log.info("Sent test job %s", job["id"])

def main():
    cookies = load_playwright_cookies()
    if not cookies:
        log.error("Aborting due to missing cookies.")
        sys.exit(1)
    conn = init_db()
    offset = None
    jobs = scrape_indeed_jobs_pw(JOB_QUERY, LOCATION, cookies, JOBS_TO_SCRAPE)
    sent = send_new_jobs(conn, jobs)
    log.info("Sent %d jobs on startup", sent)
    log.info("Polling Telegram for /test command")
    while True:
        resp = None
        try:
            import requests
            resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"timeout":20, "offset":offset}, timeout=25).json()
        except Exception as e:
            time.sleep(POLL_INTERVAL)
            continue
        if resp.get("ok"):
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if msg.get("chat", {}).get("id") == int(TELEGRAM_CHAT_ID) and msg.get("text", "").strip().lower() == "/test":
                    handle_test(conn, cookies)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main( )
