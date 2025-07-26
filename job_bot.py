#!/usr/bin/env python3
import os
import sys
import time
import logging
import sqlite3
import random
import json
import traceback
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import add_stealth
import subprocess

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
ERROR_LOG_FILE = "job_bot_errors.log"

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
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        sanitized.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": same_site,
            "expires": int(c.get("expirationDate", 0)),
        })
    return sanitized

def write_error_log(error_details):
    timestamp = datetime.utcnow().strftime("%Y‑%m‑%d %H:%M:%S UTC")
    with open(ERROR_LOG_FILE, "a", encoding="utf‑8") as f:
        f.write(f"\n\n--- ERROR OCCURRED AT {timestamp} ---\n")
        f.write(error_details)
        f.write("\n--- END ERROR ---\n")

def git_commit_and_push():
    try:
        subprocess.run(["git", "add", ERROR_LOG_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Auto commit: logged job_bot error"], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info("Error log committed and pushed to GitHub.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git commit/push failed: {e}")

def human_like_interactions(page):
    import time
    for _ in range(random.randint(5,10)):
        x = random.randint(100,1000)
        y = random.randint(100,700)
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.1,0.3))
    viewport_height = page.evaluate("window.innerHeight")
    for i in range(1,4):
        page.evaluate(f"window.scrollTo(0, {viewport_height * i / 4})")
        time.sleep(random.uniform(0.5,1.2))
    for _ in range(random.randint(1,3)):
        delta = random.randint(-100,100)
        page.evaluate(f"window.scrollBy(0, {delta})")
        time.sleep(random.uniform(0.2,0.6))
    box = page.viewport_size
    if box:
        x = random.randint(50,box["width"]-50)
        y = random.randint(50,box["height"]-50)
        page.mouse.click(x, y)
        time.sleep(random.uniform(0.3,0.7))

def scrape_indeed_jobs_pw(query, location, cookies, max_results, max_retries=3):
    from time import sleep
    from random import uniform, choice

    selectors = [
        'a.tapItem',
        'div.job_seen_beacon a',
        'a[data-jk]',
        'a[aria-label*="Job"]',
        'a[data-testid="jobTitle"]',
        'a[href*="/rc/clk?"]',
        'a.jobtitle',
    ]
    base_url = f"https://uk.indeed.com/jobs?q={query}&l={location}&radius={RADIUS_MILES}&jt=parttime"
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)... Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X... Safari/16.1)",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:115.0)... Firefox/115.0",
    ]

    for attempt in range(1, max_retries+1):
        jobs = []
        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox', '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-extensions', '--disable-gpu'
                    ]
                )
                context = browser.new_context(
                    user_agent=choice(user_agents),
                    viewport={"width":1280,"height":800},
                    java_script_enabled=True,
                    bypass_csp=True,
                    locale="en-GB",
                    timezone_id="Europe/London",
                    device_scale_factor=1,
                    is_mobile=False,
                )
                if cookies:
                    context.add_cookies(cookies)

                page = context.new_page()
                add_stealth(page)
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-GB','en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                """)
                page.goto(base_url, timeout=30000)
                human_like_interactions(page)
                sleep(uniform(2,4))

                content_lower = page.content().lower()
                captcha_detected = any([
                    "captcha" in content_lower, "verify you're human" in content_lower,
                    "recaptcha" in content_lower,
                    page.locator("input#captcha").count()>0,
                    page.locator("div.g-recaptcha").count()>0,
                    page.locator("iframe[src*='captcha']").count()>0,
                    page.locator("div#captcha").count()>0,
                ])
                if captcha_detected:
                    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    screenshot_path = f"captcha_screenshot_{now}.png"
                    html_path = f"captcha_page_{now}.html"
                    try:
                        page.screenshot(path=screenshot_path)
                    except Exception:
                        screenshot_path = "captcha screenshot failed"
                    try:
                        with open(html_path,"w",encoding="utf‑8") as f:
                            f.write(page.content())
                    except Exception:
                        html_path = "captcha html dump failed"
                    log.error(f"Captcha detected. Screenshot: {screenshot_path}, HTML: {html_path}")
                    raise RuntimeError("Blocked by Captcha or Bot detection")

                found = None
                for sel in selectors:
                    try:
                        page.wait_for_selector(sel, timeout=10000)
                        found = sel
                        break
                    except:
                        continue
                if not found:
                    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    screenshot_path = f"error_screenshot_{now}.png"
                    html_path = f"error_page_{now}.html"
                    try: page.screenshot(path=screenshot_path)
                    except: screenshot_path = "screenshot failed"
                    try:
                        with open(html_path,"w",encoding="utf‑8") as f:
                            f.write(page.content())
                    except: html_path = "html dump failed"
                    raise TimeoutError(f"No known selector found. Screenshot: {screenshot_path}, HTML: {html_path}")

                els = page.query_selector_all(found)
                for el in els:
                    if len(jobs)>=max_results: break
                    jk = el.get_attribute('data‑jk') or ""
                    if not jk:
                        href = el.get_attribute('href') or ""
                        if 'jk=' in href:
                            jk = href.split('jk=')[1].split('&')[0]
                        else:
                            continue
                    title_el = el.query_selector('h2.jobTitle span') or el.query_selector('span[title]')
                    title = title_el.inner_text().strip() if title_el else "Job"
                    jobs.append({"id": jk, "title": title, "url": f"https://uk.indeed.com/viewjob?jk={jk}"})

                browser.close()
                log.info(f"Scraped {len(jobs)} jobs on attempt {attempt}")
                return jobs

        except Exception as e:
            now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            screenshot_path = f"error_screenshot_{now}.png"
            html_path = f"error_page_{now}.html"
            try: 
                if page: page.screenshot(path=screenshot_path)
                else: screenshot_path="no page"
            except: screenshot_path="screenshot failed"
            try:
                if page:
                    with open(html_path,"w",encoding="utf‑8") as f:
                        f.write(page.content())
                else:
                    html_path="no page"
            except: html_path="html dump failed"
            tb = traceback.format_exc()
            err_msg = f"\n--- ERROR AT {datetime.utcnow().isoformat()} UTC ---\nException:\n{tb}\nURL: {page.url if page else base_url}\nScreenshot: {screenshot_path}\nHTML: {html_path}\n--- END ERROR ---"
            write_error_log(err_msg)
            git_commit_and_push()
            log.error(f"Attempt {attempt} failed: {e}")
            if browser:
                try: browser.close()
                except: pass
            if "Captcha" in str(e) or "blocked" in str(e).lower():
                log.error("Captcha/block detected – halting retries.")
                return []
            if attempt < max_retries:
                sleep_time = uniform(8, 15)
                log.info(f"Retrying in {sleep_time:.1f} seconds…")
                sleep(sleep_time)
            else:
                log.error("Max retries reached, returning empty list")
                return []

def send_telegram_message(token, chat_id, text):
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, **{"disable_web_page_preview": False, "parse_mode": "HTML"}}
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
        log.info("Sent /test job %s", job["id"])

def main():
    cookies = load_playwright_cookies()
    if not cookies:
        log.error("Aborting due to missing cookies.")
        sys.exit(1)
    conn = init_db()
    offset = None
    jobs = scrape_indeed_jobs_pw(JOB_QUERY, LOCATION, cookies, JOBS_TO_SCRAPE)
    sent = send_new_jobs(conn, jobs)
    log.info(f"Sent {sent} new jobs on startup.")
    log.info("Polling Telegram for /test command…")
    while True:
        resp = None
        try:
            import requests
            resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"timeout":20,"offset":offset}, timeout=25).json()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        if resp.get("ok"):
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if msg.get("chat", {}).get("id") == int(TELEGRAM_CHAT_ID) and msg.get("text","").strip().lower()=="/test":
                    handle_test(conn, cookies)
        time.sleep(POLL_INTERVAL)

if __name__=="__main__":
    main()
