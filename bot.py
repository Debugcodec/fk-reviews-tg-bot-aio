import os
import re
import sys
import uuid
import json
import asyncio
import sqlite3
import html as html_lib
import subprocess
from urllib.parse import quote_plus, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from PIL import Image
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# Environment & Configuration
# ---------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
REMOTE_NAME = os.getenv("REMOTE_NAME", "gdrive")
REMOTE_FOLDER = os.getenv("REMOTE_FOLDER", "TelegramBot")
REMOTE_FOLDER_ID = os.getenv("REMOTE_FOLDER_ID", "")
TARGET_KB = int(os.getenv("TARGET_KB", 50))

# Termux Home Directory Paths
HOME_DIR = os.path.expanduser("~")
DB_PATH = os.path.join(HOME_DIR, "reviews_cache.db")
TEMP_IMAGE_DIR = HOME_DIR

SORT_PARAMS = {
    "recent": "MOST_RECENT",
    "helpful": "MOST_HELPFUL",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST",
}

# ---------------------------------------------------------
# SQLite Database Cache
# ---------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid TEXT NOT NULL,
                review_id TEXT UNIQUE NOT NULL,
                author TEXT,
                rating TEXT,
                title TEXT,
                review_text TEXT,
                review_url TEXT,
                date_str TEXT,
                location TEXT
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pid ON reviews(pid)")
        conn.commit()

def save_reviews_to_db(pid: str, reviews: list):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for r in reviews:
            cursor.execute(
                """
                INSERT OR IGNORE INTO reviews 
                (pid, review_id, author, rating, title, review_text, review_url, date_str, location)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    r["reviewId"],
                    r.get("author", "Flipkart Customer"),
                    r.get("rating", "5"),
                    r.get("title", "Review"),
                    r.get("text", ""),
                    r.get("link", ""),
                    r.get("date", "Recent"),
                    r.get("location", ""),
                ),
            )
        conn.commit()

def get_cached_reviews(pid: str) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM reviews WHERE pid = ? ORDER BY id ASC", (pid,))
        rows = cursor.fetchall()
        
        cached = []
        for row in rows:
            cached.append({
                "reviewId": row["review_id"],
                "author": row["author"],
                "rating": row["rating"],
                "title": row["title"],
                "text": row["review_text"],
                "link": row["review_url"],
                "date": row["date_str"],
                "location": row["location"],
            })
        return cached

# ---------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------
def extract_pid(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "pid" in qs and qs["pid"]:
        return qs["pid"][0]
    
    match = re.search(r"pid=([A-Za-z0-9]{16})", url)
    if match:
        return match.group(1)
        
    match_any = re.search(r"([A-Za-z0-9]{16})", url)
    if match_any:
        return match_any.group(1)
        
    return ""

def extract_product_title(url: str, html_text: str, pid: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    title_elem = soup.find(["h1", "span"], class_=re.compile(r"B_NuCI|VU-Z7x|title", re.I))
    if title_elem and title_elem.get_text(strip=True):
        return title_elem.get_text(strip=True)
    
    # Try parsing title from URL slug
    parts = [p for p in urlparse(url).path.split("/") if p]
    for part in parts:
        if part not in ["product-reviews", "reviews", "p", "item"] and not re.search(r"^[a-f0-9]{10,}$", part):
            clean_title = part.replace("-", " ").title()
            if len(clean_title) > 3:
                return clean_title
                
    return f"Product ({pid})"

def fast_compress(filepath: str, target_kb: int = TARGET_KB):
    try:
        current_kb = os.path.getsize(filepath) / 1024
        if current_kb <= target_kb:
            return

        with Image.open(filepath) as img:
            img = img.convert("RGB")
            quality = 85
            for _ in range(5):
                img.save(filepath, "JPEG", optimize=True, quality=quality)
                if os.path.getsize(filepath) / 1024 <= target_kb or quality <= 25:
                    break
                quality -= 15
    except Exception as e:
        print(f"⚠️ Image compression error: {e}")

# ---------------------------------------------------------
# Web Scraping Engine
# ---------------------------------------------------------
def fetch_flipkart_reviews(pid: str, page: int = 1, sort_type: str = "recent", original_url: str = ""):
    sort_param = SORT_PARAMS.get(sort_type, "MOST_RECENT")
    url = f"https://www.flipkart.com/reviews/product/reviews/{pid}?pid={pid}&sortOrder={sort_param}&page={page}"
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.flipkart.com/",
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
    except Exception as e:
        print(f"❌ Scraper network error: {e}")
        return None, f"Request error: {e}", ""

    if response.status_code != 200:
        alt_url = f"https://www.flipkart.com/product/product-reviews/item?pid={pid}&sortOrder={sort_param}&page={page}"
        try:
            response = requests.get(alt_url, headers=headers, timeout=15)
        except Exception:
            pass

    if response.status_code != 200:
        print(f"⚠️ Scraper failed with status: {response.status_code}")
        return None, f"Status: {response.status_code}", ""

    html_text = response.text
    product_title = extract_product_title(original_url or url, html_text, pid)
    reviews_found = []
    seen_ids = set()

    # 1. Embedded JSON extraction (__NEXT_DATA__ / State trees)
    json_blobs = re.findall(r'<script[^>]*id="(?:__NEXT_DATA__|initialData)"[^>]*>(.*?)</script>', html_text, flags=re.DOTALL)
    if not json_blobs:
        json_blobs = re.findall(r'window\.__INITIAL_STATE__\s*=\s*({.*?});</script>', html_text, flags=re.DOTALL)

    def extract_from_dict(obj):
        if isinstance(obj, dict):
            r_id = obj.get("reviewId") or obj.get("id")
            text = obj.get("text") or obj.get("reviewText") or obj.get("description")
            if r_id and text and isinstance(text, str) and len(text.strip()) > 5:
                if r_id not in seen_ids:
                    seen_ids.add(r_id)
                    rating = str(obj.get("rating") or "5")
                    title = obj.get("title") or "Customer Review"
                    author = obj.get("author") or obj.get("reviewerName") or "Flipkart Customer"
                    
                    # Direct review permalink format
                    link = f"https://www.flipkart.com/reviews/product/reviews/{pid}?reviewId={r_id}"

                    reviews_found.append({
                        "reviewId": r_id,
                        "rating": rating,
                        "title": html_lib.unescape(title),
                        "text": html_lib.unescape(text),
                        "author": html_lib.unescape(author),
                        "location": "",
                        "date": "Recent",
                        "link": link,
                    })
            for v in obj.values():
                extract_from_dict(v)
        elif isinstance(obj, list):
            for item in obj:
                extract_from_dict(item)

    for blob in json_blobs:
        try:
            parsed = json.loads(blob.strip())
            extract_from_dict(parsed)
        except Exception:
            continue

    # 2. BeautifulSoup DOM extraction fallback
    if not reviews_found:
        soup = BeautifulSoup(html_text, "html.parser")
        ratings = soup.find_all(lambda tag: tag.name in ["div", "span"] and re.match(r"^[1-5](\.\d)?$", tag.get_text(strip=True)))
        
        for r_div in ratings:
            card = r_div.find_parent("div", class_=re.compile(r"col|row|_27M-vq|cPHDOP|_1AtVbE|EKF0dM"))
            if not card:
                continue
                
            card_text = card.get_text(separator=" ", strip=True)
            if len(card_text) < 25:
                continue

            r_id_match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", str(card))
            r_id = r_id_match.group(1) if r_id_match else uuid.uuid4().hex[:12]
            
            if r_id in seen_ids:
                continue
            seen_ids.add(r_id)

            title_elem = card.find(["p", "div"], class_=re.compile(r"z9E0IG|_2-N8zT|heading|title", re.I))
            text_elem = card.find(["div", "span"], class_=re.compile(r"ZmyHeo|_11XBLa|txt|text", re.I))
            author_elem = card.find(["p", "span"], class_=re.compile(r"_2NsDsF|_2sc7ZR|author", re.I))

            reviews_found.append({
                "reviewId": r_id,
                "rating": r_div.get_text(strip=True),
                "title": html_lib.unescape(title_elem.get_text(strip=True)) if title_elem else "Customer Review",
                "text": html_lib.unescape(text_elem.get_text(separator=" ", strip=True)) if text_elem else card_text[:200],
                "author": html_lib.unescape(author_elem.get_text(strip=True)) if author_elem else "Flipkart Customer",
                "location": "",
                "date": "Recent",
                "link": f"https://www.flipkart.com/reviews/product/reviews/{pid}?reviewId={r_id}",
            })

    print(f"📦 Scraped {len(reviews_found)} reviews for PID: {pid}")
    return reviews_found, pid, product_title

# ---------------------------------------------------------
# Chromium Headless Screenshot & Rclone Upload
# ---------------------------------------------------------
def take_desktop_screenshot(review_url: str) -> bytes:
    snap_path = os.path.join(TEMP_IMAGE_DIR, "temp_review_snap.png")
    if os.path.exists(snap_path):
        try:
            os.remove(snap_path)
        except Exception:
            pass

    # Termux Native Chromium command
    cmd = [
        "chromium-browser",
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1280,820",
        "--hide-scrollbars",
        "--enable-javascript",
        "--run-all-compositor-stages-before-draw",
        (
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        f"--screenshot={snap_path}",
        review_url,
    ]

    try:
        subprocess.run(cmd, timeout=25, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(snap_path) and os.path.getsize(snap_path) > 4000:
            with open(snap_path, "rb") as f:
                data = f.read()
            os.remove(snap_path)
            return data
    except Exception as e:
        print(f"⚠️ Chromium Termux capture failed: {e}")

    # Fallback to Microlink with wait delay for client rendering
    try:
        encoded_url = quote_plus(review_url)
        api_url = (
            f"https://api.microlink.io?url={encoded_url}"
            "&screenshot=true&meta=false&embed=screenshot.url"
            "&viewport.width=1280&viewport.height=820&viewport.isMobile=false"
            "&waitForTimeout=4000"
        )
        resp = requests.get(api_url, timeout=20)
        if resp.status_code == 200 and resp.content and len(resp.content) > 4000:
            return resp.content
    except Exception as e:
        print(f"⚠️ Microlink fallback error: {e}")

    return None

async def upload_screenshot_to_drive(image_bytes: bytes, filename: str = None) -> str:
    if not filename:
        filename = f"review_{uuid.uuid4().hex[:10]}.jpg"
    elif not filename.endswith(".jpg") and not filename.endswith(".png"):
        filename = f"{filename}.jpg"

    temp_path = os.path.join(TEMP_IMAGE_DIR, filename)
    target_remote_file = f"{REMOTE_NAME}:{REMOTE_FOLDER}/{filename}"
    target_remote_dir = f"{REMOTE_NAME}:{REMOTE_FOLDER}"

    try:
        with open(temp_path, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())

        fast_compress(temp_path, TARGET_KB)

        # Upload using rclone copyto
        proc = await asyncio.create_subprocess_exec(
            "rclone",
            "copyto",
            temp_path,
            target_remote_file,
            "--quiet",
            "--ignore-checksum",
            "--drive-chunk-size",
            "32M",
        )
        await proc.communicate()

        # Query exact Google Drive File ID via 'rclone lsf'
        for _ in range(5):
            await asyncio.sleep(2)
            ls_proc = await asyncio.create_subprocess_exec(
                "rclone",
                "lsf",
                target_remote_dir,
                "--include",
                filename,
                "--format",
                "i",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await ls_proc.communicate()
            file_id = stdout.decode().strip()

            if file_id and len(file_id) > 15 and " " not in file_id:
                return f"https://drive.google.com/open?id={file_id}"

    except Exception as e:
        print(f"❌ Error uploading to Drive: {e}")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    if REMOTE_FOLDER_ID:
        return f"https://drive.google.com/drive/folders/{REMOTE_FOLDER_ID}"
    return "https://drive.google.com"

# ---------------------------------------------------------
# Telegram UI & Interaction Logic
# ---------------------------------------------------------
async def render_reviews_page(update_or_query, context: ContextTypes.DEFAULT_TYPE, pid: str, page: int = 1, sort_type: str = "recent"):
    reviews = get_cached_reviews(pid)
    
    if not reviews:
        reviews, _, _ = fetch_flipkart_reviews(pid, page=1, sort_type=sort_type)
        if reviews:
            save_reviews_to_db(pid, reviews)
        else:
            msg = "❌ No reviews found for this product."
            if hasattr(update_or_query, "reply_text"):
                await update_or_query.reply_text(msg)
            else:
                await update_or_query.message.reply_text(msg)
            return

    # Pagination: 5 reviews per message view
    page_size = 5
    total_reviews = len(reviews)
    total_pages = max(1, (total_reviews + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_reviews)
    current_page_reviews = reviews[start_idx:end_idx]

    # Save session state for screenshot matching
    context.user_data["current_pid"] = pid
    context.user_data["current_page"] = page
    context.user_data["current_sort"] = sort_type
    context.user_data["review_links"] = {}

    text_parts = [
        f"📦 <b>Reviews for PID:</b> <code>{pid}</code>",
        f"📊 <b>Sorting:</b> {sort_type.title()} | <b>Page:</b> {page}/{total_pages} | <b>Total Saved:</b> {total_reviews}\n",
    ]

    inline_keyboard = []
    snap_button_row = []

    # Map cards directly to buttons 1-to-1
    for display_idx, r in enumerate(current_page_reviews, start=1):
        r_id = r["reviewId"]
        r_url = r["link"]
        if not r_url or ":1?reviewId=" in r_url:
            r_url = f"https://www.flipkart.com/reviews/product/reviews/{pid}?reviewId={r_id}"

        context.user_data["review_links"][r_id] = r_url

        card_text = (
            f"<b>#{display_idx}</b> | ⭐ <b>Rating:</b> {r['rating']}/5\n"
            f"👤 <b>Author:</b> {r['author']}\n"
            f"📌 <b>Title:</b> {r['title']}\n"
            f"💬 <b>Review:</b> {r['text'][:300]}...\n"
            f"🔗 <a href='{r_url}'>Direct Review Link</a>\n"
        )
        text_parts.append(card_text)

        snap_button_row.append(
            InlineKeyboardButton(f"📸 Screenshot #{display_idx}", callback_data=f"snap_{r_id}")
        )
        if len(snap_button_row) == 2:
            inline_keyboard.append(snap_button_row)
            snap_button_row = []

    if snap_button_row:
        inline_keyboard.append(snap_button_row)

    # Sort filter buttons
    inline_keyboard.append([
        InlineKeyboardButton("💡 Helpful", callback_data=f"sort_helpful_{pid}"),
        InlineKeyboardButton("🕒 Latest", callback_data=f"sort_recent_{pid}"),
    ])
    inline_keyboard.append([
        InlineKeyboardButton("⭐ Positive", callback_data=f"sort_positive_{pid}"),
        InlineKeyboardButton("⚠️ Negative", callback_data=f"sort_negative_{pid}"),
    ])

    # Navigation buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav_{page - 1}_{pid}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav_{page + 1}_{pid}"))
    
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(inline_keyboard)
    full_message_text = "\n".join(text_parts)

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(
            full_message_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    else:
        await update_or_query.reply_text(
            full_message_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

# ---------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Welcome!</b>\n\n"
        "Send me any Flipkart or Shopsy product link to fetch real customer reviews, capture verified screenshots, and upload them directly to Google Drive.",
        parse_mode=ParseMode.HTML,
    )

async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    pid = extract_pid(text)
    
    if not pid:
        await update.message.reply_text("❌ Could not extract a valid Product ID (PID) from that link.")
        return

    status_msg = await update.message.reply_text("🔍 Extracting product reviews...")
    await render_reviews_page(update.message, context, pid, page=1, sort_type="recent")
    try:
        await status_msg.delete()
    except Exception:
        pass

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("snap_"):
        review_id = data.split("_", 1)[1]
        links_dict = context.user_data.get("review_links", {})
        review_url = links_dict.get(review_id)

        # Lookup in SQLite cache if link is missing from active memory
        if not review_url:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("SELECT review_url, pid FROM reviews WHERE review_id = ?", (review_id,))
                row = c.fetchone()
                if row and row[0]:
                    review_url = row[0]
                elif row and row[1]:
                    review_url = f"https://www.flipkart.com/reviews/product/reviews/{row[1]}?reviewId={review_id}"

        # General dynamic fallback
        if not review_url:
            current_pid = context.user_data.get("current_pid")
            if current_pid:
                review_url = f"https://www.flipkart.com/reviews/product/reviews/{current_pid}?reviewId={review_id}"

        if not review_url:
            await query.message.reply_text("❌ Review reference expired. Send the product link again.")
            return

        status_msg = await query.message.reply_text("📸 Capturing screenshot via Chromium...")

        # Run headless Chromium screenshot in background thread
        img_bytes = await asyncio.to_thread(take_desktop_screenshot, review_url)

        if not img_bytes:
            await status_msg.edit_text("❌ Failed to render and capture screenshot.")
            return

        await status_msg.edit_text("☁️ Uploading screenshot to Google Drive...")
        
        filename = f"review_{review_id[:12]}.jpg"
        drive_link = await upload_screenshot_to_drive(img_bytes, filename=filename)

        caption_text = (
            f"🔗 <a href='{drive_link}'>Google Drive Link</a>\n"
            f"🔗 <a href='{review_url}'>Review Link</a>"
        )

        await query.message.reply_photo(
            photo=img_bytes,
            caption=caption_text,
            parse_mode=ParseMode.HTML,
        )
        try:
            await status_msg.delete()
        except Exception:
            pass

    elif data.startswith("sort_"):
        parts = data.split("_")
        sort_type = parts[1]
        pid = parts[2]
        await render_reviews_page(query, context, pid, page=1, sort_type=sort_type)

    elif data.startswith("nav_"):
        parts = data.split("_")
        page = int(parts[1])
        pid = parts[2]
        sort_type = context.user_data.get("current_sort", "recent")
        await render_reviews_page(query, context, pid, page=page, sort_type=sort_type)

# ---------------------------------------------------------
# Main Application Entry Point
# ---------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN is missing from your .env configuration.")
        sys.exit(1)

    init_db()
    print("🚀 Initializing Bot with Termux Chromium & SQLite Local Cache...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("✅ Bot is online and listening for Telegram updates.")
    app.run_polling()

if __name__ == "__main__":
    main()
