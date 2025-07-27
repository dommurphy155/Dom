#!/usr/bin/env python3
import os, sys, time, json, asyncio, logging, sqlite3, traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from playwright.sync_api import sync_playwright
from stealth import apply_stealth_sync

# Load config
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
COOKIES_FILE = "cookies.json"
DB_PATH = "jobs_sent.db"
QUERY = "part time"
LOCATION = "Leigh"
MAX_DISTANCE = 5  # miles
SCRAPE_LIMIT = 33
SEND_LIMIT = 8
SEND_TIMES = ["10:30", "17:30", "21:00"]

# Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger()

# Bot init
bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# DB init
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS sent_jobs (job_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def already_sent(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_jobs WHERE job_id = ?", (job_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_sent(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_jobs (job_id) VALUES (?)", (job_id,))
    conn.commit()
    conn.close()

# Scraper
def scrape_jobs():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            if os.path.exists(COOKIES_FILE):
                with open(COOKIES_FILE, "r") as f:
                    context.add_cookies(json.load(f))
            page = context.new_page()
            apply_stealth_sync(page)
            log.info("Loading Indeed search...")
            page.goto(f"https://uk.indeed.com/jobs?q={QUERY.replace(' ', '+')}&l={LOCATION}&radius={MAX_DISTANCE}", timeout=30000)
            page.wait_for_selector("a[data-jk]", timeout=10000)
            job_elements = page.query_selector_all("a[data-jk]")[:SCRAPE_LIMIT]
            jobs = []
            for el in job_elements:
                try:
                    jk = el.get_attribute("data-jk")
                    title = el.inner_text().strip()
                    href = f"https://uk.indeed.com/viewjob?jk={jk}"
                    if not already_sent(jk):
                        jobs.append((jk, title, href))
                except Exception:
                    continue
            browser.close()
            return jobs[:SEND_LIMIT]
    except Exception as e:
        log.error("Scraping failed: " + str(e))
        return []

# Telegram Handlers
@dp.message_handler(commands=["test"])
async def handle_test(msg: types.Message):
    jobs = scrape_jobs()
    if jobs:
        await send_job(jobs[0])
    else:
        await msg.reply("No jobs found.")

@dp.callback_query_handler(lambda c: c.data.startswith("accept_") or c.data.startswith("decline_"))
async def handle_action(callback: types.CallbackQuery):
    action, job_id = callback.data.split("_", 1)
    await callback.answer()
    if action == "accept":
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("👍 Marked accepted.")
    else:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply("❌ Deleted.")

async def send_job(job):
    jk, title, url = job
    text = f"💼 <b>{title}</b>\n🔗 {url}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Accept", callback_data=f"accept_{jk}"))
    markup.add(types.InlineKeyboardButton("❌ Decline", callback_data=f"decline_{jk}"))
    await bot.send_message(CHAT_ID, text, reply_markup=markup, parse_mode="HTML")
    mark_sent(jk)

async def scheduled_send():
    while True:
        now = datetime.now().strftime("%H:%M")
        if now in SEND_TIMES:
            log.info(f"Scheduled send at {now}")
            jobs = scrape_jobs()
            for job in jobs:
                await send_job(job)
            await asyncio.sleep(60)  # avoid duplicate sends
        await asyncio.sleep(20)

# Main
if __name__ == "__main__":
    try:
        init_db()
        loop = asyncio.get_event_loop()
        loop.create_task(scheduled_send())
        executor.start_polling(dp, skip_updates=True)
    except Exception:
        log.error(traceback.format_exc())
        sys.exit(1)
