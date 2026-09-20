import asyncio
from concurrent.futures import ThreadPoolExecutor
import html as html_lib
import json
import os
import re
import sqlite3
import subprocess
import time
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse
import uuid

from bs4 import BeautifulSoup
from curl_cffi import requests
from dotenv import load_dotenv
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load environment variables
load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMOTE_NAME = os.getenv("REMOTE_NAME", "gdrive")
REMOTE_FOLDER = os.getenv("REMOTE_FOLDER", "TelegramBot")
REMOTE_FOLDER_ID = os.getenv("REMOTE_FOLDER_ID", "")
TARGET_KB = int(os.getenv("TARGET_KB", 50))
REVIEW_BASE_URL = "https://www.flipkart.com/wr/write-review/wr?source=orderDetails&pid="
DB_PATH = os.path.join(os.path.expanduser("~"), "reviews_cache.db")

CACHED_FOLDER_ID = REMOTE_FOLDER_ID

SORT_PARAMS = {
    "helpful": "MOST_HELPFUL",
    "recent": "MOST_RECENT",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST",
}


# ==========================================
# DATABASE LAYER (SQLITE)
# ==========================================
def init_db():
  with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.cursor()
    cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                pid TEXT,
                product_title TEXT,
                rating TEXT,
                title TEXT,
                text TEXT,
                author TEXT,
                location TEXT,
                date TEXT,
                link TEXT,
                sort_tag TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pid ON reviews(pid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_author ON reviews(author COLLATE NOCASE)")
    conn.commit()


def save_reviews_to_db(pid: str, product_title: str, reviews: list, sort_tag: str = "recent"):
  with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.cursor()
    for r in reviews:
      cursor.execute(
          """
                INSERT OR REPLACE INTO reviews
                (review_id, pid, product_title, rating, title, text, author, location, date, link, sort_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
          (
              r.get("reviewId"),
              pid,
              product_title,
              r.get("rating"),
              r.get("title"),
              r.get("text"),
              r.get("author"),
              r.get("location"),
              r.get("date"),
              r.get("link"),
              sort_tag,
          ),
      )
    conn.commit()


def get_cached_reviews(pid: str, page: int = 1, per_page: int = 5):
  offset = (page - 1) * per_page
  with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT * FROM reviews
            WHERE pid = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """,
        (pid, per_page, offset),
    )
    return [dict(row) for row in cursor.fetchall()]


def count_cached_reviews(pid: str) -> int:
  with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM reviews WHERE pid = ?", (pid,))
    res = cursor.fetchone()
    return res[0] if res else 0


def search_author_in_db(pid: str, query_str: str):
  q = f"%{query_str.strip()}%"
  with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
            SELECT * FROM reviews
            WHERE pid = ? AND (author LIKE ? OR text LIKE ? OR title LIKE ?)
            ORDER BY created_at DESC
        """,
        (pid, q, q, q),
    )
    return [dict(row) for row in cursor.fetchall()]


# ==========================================
# RCLONE & DRIVE HELPERS
# ==========================================
def fast_compress(input_path, target_kb):
  try:
    with Image.open(input_path) as img:
      if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
      if img.width > 1024:
        img.thumbnail((1024, 1024), Image.Resampling.BILINEAR)
      img.save(input_path, "JPEG", quality=50, optimize=False)
  except Exception:
    pass


async def upload_screenshot_to_drive(image_bytes: bytes, filename: str = None) -> str:
    if not filename:
        filename = f"review_{uuid.uuid4().hex[:10]}.jpg"
    elif not filename.endswith(".jpg") and not filename.endswith(".png"):
        filename = f"{filename}.jpg"

    temp_path = os.path.join(os.path.expanduser("~"), filename)
    target_remote_file = f"{REMOTE_NAME}:{REMOTE_FOLDER}/{filename}"

    try:
        # 1. Write file locally
        with open(temp_path, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())

        try:
            fast_compress(temp_path, 300)
        except Exception:
            pass

        # 2. Upload file via rclone
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

        # 3. Retry rclone link to allow Drive permissions to register
        for attempt in range(3):
            await asyncio.sleep(1.5)
            link_proc = await asyncio.create_subprocess_exec(
                "rclone",
                "link",
                target_remote_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await link_proc.communicate()
            link = stdout.decode().strip()

            if link.startswith("http") and "/folders/" not in link:
                return link

       # 4. Fallback: Fetch exact file ID directly using rclone lsjson
        ls_proc = await asyncio.create_subprocess_exec(
            "rclone",
            "lsjson",
            target_remote_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await ls_proc.communicate()
        raw_output = stdout.decode().strip()
        
              # Only parse if output looks like a valid JSON array
        if raw_output.startswith("["):
            try:
                items = json.loads(raw_output)
                if items and "ID" in items[0]:
                    return f"https://drive.google.com/open?id={items[0]['ID']}"
            except Exception:
                pass

    except Exception as e:
        print(f"❌ Error uploading/getting link: {e}")

    return f"https://drive.google.com/drive/search?q={filename}" 
                

async def upload_worker(
    file_path,
    file_name,
    chat_id,
    message_id,
    context,
    start_time,
    original_size,
    status_msg_id,
):
  attempt = 1
  if file_path.lower().endswith((".jpg", ".jpeg", ".png")):
    await asyncio.to_thread(fast_compress, file_path, TARGET_KB)

  compressed_size = (
      os.path.getsize(file_path) / 1024 if os.path.exists(file_path) else original_size
  )
  target_remote_file = f"{REMOTE_NAME}:{REMOTE_FOLDER}/{file_name}"

  while True:
    try:
      if not os.path.exists(file_path):
        raise Exception("Local file missing.")

      move_proc = await asyncio.create_subprocess_exec(
          "rclone",
          "copyto",
          file_path,
          target_remote_file,
          "--quiet",
          "--ignore-checksum",
          "--drive-chunk-size",
          "64M",
          "--transfers",
          "8",
          "--checkers",
          "8",
          "--buffer-size",
          "32M",
      )
      await asyncio.wait_for(move_proc.wait(), timeout=25)

      if move_proc.returncode != 0:
        raise Exception(f"Rclone exited with code {move_proc.returncode}")

      link_proc = await asyncio.create_subprocess_exec(
          "rclone",
          "link",
          target_remote_file,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
      )
      stdout, _ = await asyncio.wait_for(link_proc.communicate(), timeout=8)
      drive_link = stdout.decode().strip()

      if not drive_link or not drive_link.startswith("http"):
        drive_link = f"https://drive.google.com/drive/folders/{REMOTE_FOLDER_ID}"

      duration = round(time.time() - start_time, 2)
      text = (
          f"✅ <b>Upload Success!</b>\n\n"
          f"📊 <b>Size:</b> {original_size:.1f}KB → {compressed_size:.1f}KB\n"
          f"⏱️ <b>Time:</b> {duration}s"
      )

      keyboard = InlineKeyboardMarkup(
          [[InlineKeyboardButton("🔗 Open Google Drive Link", url=drive_link)]]
      )
      await context.bot.edit_message_text(
          chat_id=chat_id,
          message_id=status_msg_id,
          text=text,
          parse_mode=ParseMode.HTML,
          reply_markup=keyboard,
      )

      if os.path.exists(file_path):
        os.remove(file_path)
      return

    except Exception as e:
      print(f"⚠️ Upload Attempt {attempt} failed: {str(e)}")
      attempt += 1
      try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=f"🔄 <b>Uploading to Drive... (Retry {attempt})</b>",
            parse_mode=ParseMode.HTML,
        )
      except Exception:
        pass
      await asyncio.sleep(1.5)


# ==========================================
# SCREENSHOT ENGINE (TERMUX CHROMIUM)
# ==========================================
def take_desktop_screenshot(review_url: str) -> bytes:
  snap_path = "/data/data/com.termux/files/home/temp_review_snap.png"
  if os.path.exists(snap_path):
    try:
      os.remove(snap_path)
    except Exception:
      pass

  cmd = [
      "chromium-browser",
      "--headless",
      "--disable-gpu",
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-software-rasterizer",
      "--disable-features=DBus",
      "--disable-background-networking",
      "--window-size=1280,820",
      "--virtual-time-budget=2000",
      "--run-all-compositor-stages-before-draw",
      (
          "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
          " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0"
          " Safari/537.36"
      ),
      f"--screenshot={snap_path}",
      review_url,
  ]

  try:
    subprocess.run(cmd, timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.exists(snap_path) and os.path.getsize(snap_path) > 10000:
      with open(snap_path, "rb") as f:
        data = f.read()
      os.remove(snap_path)
      return data
  except Exception as e:
    print(f"⚠️ Termux Chromium capture failed: {e}")

  try:
    encoded_url = quote_plus(review_url)
    api_endpoint = (
        f"https://api.microlink.io?url={encoded_url}"
        "&screenshot=true&meta=false&embed=screenshot.url"
        "&viewport.width=1280&viewport.height=820&viewport.isMobile=false"
        "&waitForTimeout=3000"
    )
    resp = requests.get(api_endpoint, timeout=18)
    if resp.status_code == 200 and resp.content:
      return resp.content
  except Exception as e:
    print(f"⚠️ Microlink fallback error: {e}")

  return None


def clean_location(loc_obj) -> str:
  if not loc_obj:
    return ""
  if isinstance(loc_obj, dict):
    city = loc_obj.get("city") or loc_obj.get("cityName") or ""
    state = loc_obj.get("state") or loc_obj.get("stateName") or ""
    parts = [p.strip() for p in [city, state] if p and str(p).strip()]
    return ", ".join(parts)
  if isinstance(loc_obj, str):
    return loc_obj.strip()
  return ""


def clean_date_string(raw_date: str) -> str:
  if not raw_date:
    return ""
  d = str(raw_date).strip()
  d = re.sub(r"^Verified Purchase\s*[,•·]?\s*", "", d, flags=re.IGNORECASE)
  d = re.sub(r"^Certified Buyer\s*[,•·]?\s*", "", d, flags=re.IGNORECASE)
  return d.strip()


def extract_author_name(node) -> str:
  if not node:
    return "Flipkart Customer"
  if isinstance(node, dict):
    name = (
        node.get("name")
        or node.get("authorName")
        or node.get("reviewerName")
        or node.get("text")
    )
    if name and isinstance(name, str) and name.strip():
      return html_lib.unescape(name.strip())
  if isinstance(node, str) and node.strip():
    if not node.startswith("{") and "Location" not in node:
      return html_lib.unescape(node.strip())
  return "Flipkart Customer"


def extract_product_title(original_url: str, html_text: str, fallback_pid: str) -> str:
  slug_match = re.search(r"(?:flipkart|shopsy)\.(?:com|in)/([^/?#]+)/", original_url)
  if slug_match:
    slug = slug_match.group(1)
    if slug not in ["product", "product-reviews", "reviews", "p"]:
      clean_name = " ".join(word.capitalize() for word in slug.split("-") if word)
      if len(clean_name) > 3:
        return clean_name

  match_title = re.search(r'"productTitle"\s*:\s*"([^"]+)"|"title"\s*:\s*"([^"]+)"', html_text)
  if match_title:
    cand = match_title.group(1) or match_title.group(2)
    if cand and "Review" not in cand and "Flipkart" not in cand and len(cand) > 5:
      return html_lib.unescape(cand).strip()

  return f"Product ({fallback_pid})"


# ==========================================
# SCRAPING ENGINE
# ==========================================
def fetch_flipkart_reviews(
    pid: str,
    page: int = 1,
    sort_type: str = "recent",
    original_url: str = "",
):
  sort_param = SORT_PARAMS.get(sort_type, "MOST_RECENT")
  url = f"https://www.flipkart.com/product/product-reviews/item?pid={pid}&sortOrder={sort_param}&page={page}"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
          " Chrome/124.0.0.0 Safari/537.36"
      ),
      "Accept": (
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
      ),
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": f"https://www.flipkart.com/product/product-reviews/item?pid={pid}",
  }
  try:
    response = requests.get(url, headers=headers, impersonate="chrome120", timeout=8)
  except Exception as e:
    return None, f"Request error: {e}", ""

  if response.status_code != 200:
    return None, f"Status: {response.status_code}", ""

  html_text = response.text
  product_title = extract_product_title(original_url or url, html_text, pid)
  reviews_found = []
  seen_ids = set()

  soup = BeautifulSoup(html_text, "html.parser")
  card_containers = soup.find_all(
      "div",
      class_=re.compile(r"cPHDOP|col-12-12|_27M-vq|col _2w2M|row _200c3e|_1AtVbE|EKF0dM"),
  )

  for card in card_containers:
    card_html = str(card)
    r_id_match = re.search(
        r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        card_html,
    )
    if not r_id_match:
      continue

    r_id = r_id_match.group(1)
    if r_id in seen_ids:
      continue

    link_elem = card.find("a", href=re.compile(r"/reviews/|\?reviewId=", re.IGNORECASE))
    if link_elem and "href" in link_elem.attrs:
      raw_href = link_elem["href"]
      review_link = (
          f"https://www.flipkart.com{raw_href}" if not raw_href.startswith("http") else raw_href
      )
    else:
      review_link = f"https://www.flipkart.com/reviews/{pid}:{len(seen_ids)+1}?reviewId={r_id}"

    rating_elem = card.find("div", class_=re.compile(r"XDXgAE|_3LWZlK|_1BLPMq"))
    title_elem = card.find("p", class_=re.compile(r"z9E0IG|_2-N8zT"))
    text_elem = card.find("div", class_=re.compile(r"ZmyHeo|_11XBLa|txt|row _3nLaff"))
    author_elem = card.find("p", class_=re.compile(r"_2NsDsF|_2sc7ZR"))

    date_elem = card.find("p", class_=re.compile(r"_3c7q|wX4Z|M4Vp|_2mcPpE|_2_R_DZ"))
    post_date = ""
    if date_elem:
      post_date = clean_date_string(date_elem.get_text(strip=True))
    else:
      time_match = re.search(
          r"(\d+\s+(?:day|days|month|months|year|years|hour|hours|min|mins)\s+ago|Today|Yesterday)",
          card.get_text(),
      )
      if time_match:
        post_date = time_match.group(1)

    author_str = "Flipkart Customer"
    loc_str = ""
    if author_elem:
      raw_author_text = author_elem.get_text(strip=True)
      if "," in raw_author_text:
        parts = raw_author_text.split(",", 1)
        author_str = parts[0].strip()
        loc_str = parts[1].strip()
      else:
        author_str = raw_author_text

    seen_ids.add(r_id)
    reviews_found.append({
        "reviewId": r_id,
        "rating": (
            rating_elem.get_text(strip=True)
            if rating_elem and rating_elem.get_text(strip=True)
            else "5.0"
        ),
        "title": (
            html_lib.unescape(title_elem.get_text(strip=True)) if title_elem else "Verified Review"
        ),
        "text": (
            html_lib.unescape(text_elem.get_text(separator=" ", strip=True))
            if text_elem
            else "No Review Text"
        ),
        "author": html_lib.unescape(author_str),
        "location": loc_str,
        "date": post_date,
        "link": review_link,
    })

  def walk_json(node):
    if isinstance(node, dict):
      has_content = any(k in node for k in ["text", "description", "reviewText", "title"])
      has_meta = any(k in node for k in ["rating", "author", "authorName", "reviewerName"])

      if ("reviewId" in node or "id" in node) and has_content and has_meta:
        r_id = node.get("reviewId") or node.get("id")
        if (
            isinstance(r_id, str)
            and re.match(r"^[a-f0-9\-]{36}$", r_id)
            and r_id not in seen_ids
        ):
          seen_ids.add(r_id)
          raw_url = (
              node.get("url")
              or node.get("reviewUrl")
              or f"/reviews/{pid}:1?reviewId={r_id}"
          )
          full_permalink = (
              f"https://www.flipkart.com{raw_url}" if not raw_url.startswith("http") else raw_url
          )
          reviews_found.append({
              "reviewId": r_id,
              "rating": (
                  node.get("rating")
                  or node.get("starRating")
                  or node.get("userRating", "5.0")
              ),
              "title": html_lib.unescape(str(node.get("title") or node.get("heading", "No Title"))),
              "text": html_lib.unescape(
                  str(
                      node.get("text")
                      or node.get("description")
                      or node.get("reviewText", "No Text")
                  )
              ),
              "author": extract_author_name(
                  node.get("author") or node.get("authorName") or node.get("reviewerName")
              ),
              "location": clean_location(node.get("location") or node.get("reviewerLocation")),
              "date": clean_date_string(
                  node.get("created")
                  or node.get("reviewDate")
                  or node.get("submissionTime")
                  or ""
              ),
              "link": full_permalink,
          })
      for v in node.values():
        walk_json(v)
    elif isinstance(node, list):
      for item in node:
        walk_json(item)

  scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text, flags=re.DOTALL)
  for script in scripts:
    script = script.strip()
    if "reviewId" in script:
      for raw in re.findall(r"=\s*(\{.*?\})\s*;", script, re.DOTALL):
        try:
          walk_json(json.loads(raw))
        except Exception:
          pass
      try:
        walk_json(json.loads(script))
      except Exception:
        pass

  return reviews_found, pid, product_title


# ==========================================
# CRAWLER HELPER (PULLS & CACHES PAGES)
# ==========================================
def crawl_and_cache_product(
    pid: str, max_pages: int = 5, sort_type: str = "recent", original_url: str = ""
):
  all_scraped = []
  final_title = f"Product ({pid})"
  for p in range(1, max_pages + 1):
    revs, _, title = fetch_flipkart_reviews(pid, p, sort_type, original_url)
    if title and "Product (" not in title:
      final_title = title
    if not revs:
      break
    all_scraped.extend(revs)
    if len(revs) < 5:
      break
  if all_scraped:
    save_reviews_to_db(pid, final_title, all_scraped, sort_type)
  return all_scraped, final_title


# ==========================================
# MESSAGE & KEYBOARD BUILDER
# ==========================================
def build_review_message(
    reviews,
    pid: str,
    product_title: str,
    page: int,
    sort_type: str,
    per_page: int = 5,
    search_query: str = "",
    converted_info: str = "",
    total_cached: int = 0,
):
  sort_labels = {
      "recent": "🕒 Latest",
      "helpful": "💡 Most Helpful",
      "positive": "⭐ Positive",
      "negative": "⚠️ Negative",
  }

  text = ""
  if converted_info:
    text += converted_info + "\n━━━━━━━━━━━━━━━━━━━━\n"

  text += f"📦 <b>Product:</b> <b>{html_lib.escape(product_title)}</b>\n"
  if search_query:
    text += (
        f"🔍 <b>Local Author Search:</b> <i>{html_lib.escape(search_query)}</i>"
        f" (Found: {len(reviews)})\n"
    )
  else:
    count_str = f" | Total Saved: {total_cached}" if total_cached > 0 else ""
    text += f"📊 <b>Sorting:</b> {sort_labels.get(sort_type, 'Latest')} | <b>Page:</b> {page}{count_str}\n"
  text += "━━━━━━━━━━━━━━━━━━━━\n\n"

  if not reviews:
    text += "❌ <i>No reviews found in local database.</i>\n"
    return (
        text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Crawl/Sync From Web", callback_data=f"sync_{pid}")],
            [InlineKeyboardButton("🔍 Search Another Author", callback_data=f"search_{pid}")],
        ]),
    )

  display_reviews = reviews[:10] if search_query else reviews[:per_page]
  keyboard = []
  screenshot_buttons = []

  for idx, r in enumerate(display_reviews, 1):
    safe_author = html_lib.escape(str(r["author"]))
    safe_title = html_lib.escape(str(r["title"]))
    safe_text = html_lib.escape(str(r["text"][:250]))
    safe_rating = html_lib.escape(str(r["rating"]))
    safe_location = html_lib.escape(str(r.get("location", "")))
    safe_date = html_lib.escape(str(r.get("date", "")))
    link = r.get("link")

    if safe_location:
      user_line = f"👤 <b>Author:</b> <b>{safe_author}</b> (📍 <i>{safe_location}</i>)\n"
    else:
      user_line = f"👤 <b>Author:</b> <b>{safe_author}</b>\n"

    date_line = f"🕒 <b>Posted:</b> <i>{safe_date}</i>\n" if safe_date else ""
    text += (
        f"<b>#{idx}</b> | ⭐ <b>Rating:</b> {safe_rating} / 5\n"
        f"{user_line}"
        f"{date_line}"
        f"📌 <b>Title:</b> {safe_title}\n"
        f"💬 <b>Review:</b> {safe_text}...\n"
        f'🔗 <a href="{link}">Direct Review Link</a>\n'
        "────────────────────\n"
    )

    r_id = r.get("review_id") or r.get("reviewId")
    screenshot_buttons.append(
        InlineKeyboardButton(f"📸 Screenshot #{idx}", callback_data=f"snap_{r_id}")
    )

  for i in range(0, len(screenshot_buttons), 2):
    keyboard.append(screenshot_buttons[i : i + 2])

  keyboard.append([
      InlineKeyboardButton("💡 Helpful", callback_data=f"rev_{pid}_1_helpful"),
      InlineKeyboardButton("🕒 Latest", callback_data=f"rev_{pid}_1_recent"),
  ])
  keyboard.append([
      InlineKeyboardButton("⭐ Positive", callback_data=f"rev_{pid}_1_positive"),
      InlineKeyboardButton("⚠️ Negative", callback_data=f"rev_{pid}_1_negative"),
  ])
  keyboard.append([
      InlineKeyboardButton("🔍 Search Author", callback_data=f"search_{pid}"),
      InlineKeyboardButton("🔄 Sync Web", callback_data=f"sync_{pid}"),
  ])

  nav_row = []
  if page > 1 and not search_query:
    nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"rev_{pid}_{page - 1}_{sort_type}"))

  if len(reviews) >= per_page and not search_query:
    nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"rev_{pid}_{page + 1}_{sort_type}"))

  if nav_row:
    keyboard.append(nav_row)

  return text, InlineKeyboardMarkup(keyboard)


async def delete_saved_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
  msg_ids = context.user_data.get("messages_to_clean", [])
  for m_id in msg_ids:
    try:
      await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
    except Exception:
      pass
  context.user_data["messages_to_clean"] = []


# ==========================================
# BOT DISPATCHERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  await update.message.reply_text(
      "👋 <b>Welcome!</b>\n\n"
      "• Send a <b>Flipkart / Shopsy product link</b> to parse, cache in local"
      " DB, search authors, and capture desktop views.\n"
      "• Send <b>Images / Photos</b> to auto-compress and upload directly to"
      " Google Drive via Rclone.",
      parse_mode=ParseMode.HTML,
  )


async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
  chat_id = update.effective_chat.id
  user_text = update.message.text.strip()

  if context.user_data.get("awaiting_author_search"):
    pid = context.user_data.get("search_pid")
    product_title = context.user_data.get("product_title", f"Product ({pid})")
    prompt_msg_id = context.user_data.get("prompt_msg_id")
    context.user_data["awaiting_author_search"] = False

    matched_reviews = search_author_in_db(pid, user_text)

    if not matched_reviews:
      status_msg = await update.message.reply_text(
          f"🔍 Not in local cache, scanning web for '<b>{html_lib.escape(user_text)}</b>'...",
          parse_mode=ParseMode.HTML,
      )
      loop = asyncio.get_running_loop()
      await loop.run_in_executor(None, crawl_and_cache_product, pid, 10, "recent")
      matched_reviews = search_author_in_db(pid, user_text)
      try:
        await status_msg.delete()
      except Exception:
        pass

    links_dict = context.user_data.setdefault("review_links", {})
    for r in matched_reviews:
      r_id = r.get("review_id") or r.get("reviewId")
      links_dict[r_id] = r["link"]

    await delete_saved_messages(context, chat_id)
    if prompt_msg_id:
      try:
        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
      except Exception:
        pass

    msg_text, reply_markup = build_review_message(
        matched_reviews,
        pid,
        product_title,
        1,
        "recent",
        per_page=10,
        search_query=user_text,
    )

    sent_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.user_data["messages_to_clean"] = [sent_msg.message_id]
    return

  if "flipkart.com" in user_text or "shopsy.in" in user_text:
    url_match = re.search(r"(https?://\S+)", user_text)
    if not url_match:
      return

    url = url_match.group(1)
    parsed = urlparse(url)
    pid = parse_qs(parsed.query).get("pid", [None])[0] or re.search(r"pid=([A-Z0-9]+)", url)
    if isinstance(pid, re.Match):
      pid = pid.group(1)

    if not pid:
      await update.message.reply_text("❌ Could not find a valid `pid` in the provided link.")
      return

    new_netloc = "www.shopsy.in" if "flipkart.com" in parsed.netloc else "www.flipkart.com"
    target_label = "Shopsy" if "flipkart.com" in parsed.netloc else "Flipkart"
    new_query = urlencode({"pid": pid}) if pid else ""
    converted_url = urlunparse((
        parsed.scheme,
        new_netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))

    converted_info = (
        f"🔗 <b>{target_label} Link:</b>\n{converted_url}\n\n"
        f"📝 <b>Write Review Link:</b>\n{REVIEW_BASE_URL}{pid}"
    )

    cached_reviews = get_cached_reviews(pid, page=1, per_page=5)
    total_count = count_cached_reviews(pid)

    if cached_reviews:
      product_title = cached_reviews[0].get("product_title", f"Product ({pid})")
      reviews = cached_reviews
    else:
      status_msg = await update.message.reply_text(
          "📥 <i>Product not yet cached. Crawling initial reviews...</i>",
          parse_mode=ParseMode.HTML,
      )
      loop = asyncio.get_running_loop()
      scraped_reviews, product_title = await loop.run_in_executor(
          None, crawl_and_cache_product, pid, 3, "recent", url
      )
      try:
        await status_msg.delete()
      except Exception:
        pass
      reviews = get_cached_reviews(pid, page=1, per_page=5)
      total_count = count_cached_reviews(pid)

    if not reviews:
      await update.message.reply_text(
          f"📦 <b>Product:</b> <b>{html_lib.escape(product_title)}</b>\n\n"
          f"{converted_info}\n\n❌ <i>No reviews found online.</i>",
          parse_mode=ParseMode.HTML,
          disable_web_page_preview=True,
      )
      return

    links_dict = context.user_data.setdefault("review_links", {})
    for r in reviews:
      r_id = r.get("review_id") or r.get("reviewId")
      links_dict[r_id] = r["link"]

    context.user_data["product_title"] = product_title
    context.user_data["original_url"] = url
    context.user_data["converted_info"] = converted_info

    await delete_saved_messages(context, chat_id)

    msg_text, reply_markup = build_review_message(
        reviews,
        pid,
        product_title,
        1,
        "recent",
        per_page=5,
        converted_info=converted_info,
        total_cached=total_count,
    )

    sent_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.user_data["messages_to_clean"] = [sent_msg.message_id]


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
      try:
          await query.answer()
      except Exception:
          pass
    

  data = query.data

  if data.startswith("snap_"):
    review_id = data.split("_", 1)[1]
    links_dict = context.user_data.get("review_links", {})
    review_url = links_dict.get(review_id)

    if not review_url:
      with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT link FROM reviews WHERE review_id = ?", (review_id,))
        row = c.fetchone()
        if row:
          review_url = row[0]

    if not review_url:
      await query.message.reply_text("❌ Review reference expired. Please search or reload again.")
      return

    status_msg = await query.message.reply_text(
        "📸 <i>Rendering snapshot...</i>", parse_mode=ParseMode.HTML
    )

    loop = asyncio.get_running_loop()
    image_bytes = await loop.run_in_executor(None, take_desktop_screenshot, review_url)

    try:
      await status_msg.delete()
    except Exception:
      pass

    if not image_bytes:
      await query.message.reply_text(
          "❌ Could not capture browser screenshot. Please check the review link directly."
      )
      return

    safe_review_url = html_lib.escape(review_url)
    initial_caption = (
        "🔗 <i>Uploading to Google Drive...</i>\n"
        f'🔗 <a href="{safe_review_url}">Review Link</a>'
    )
    sent_photo = await query.message.reply_photo(
        photo=image_bytes, caption=initial_caption, parse_mode=ParseMode.HTML
    )

    async def upload_and_edit_caption(photo_msg, raw_bytes, r_url):
      filename = f"review_{uuid.uuid4().hex[:8]}.jpg"
      drive_link = await upload_screenshot_to_drive(raw_bytes, filename)
      safe_drive = html_lib.escape(drive_link)
      safe_rev = html_lib.escape(r_url)
      updated_caption = (
          f'🔗 <a href="{safe_drive}">Google Drive Link</a>\n'
          f'🔗 <a href="{safe_rev}">Review Link</a>'
      )
      try:
        await photo_msg.edit_caption(caption=updated_caption, parse_mode=ParseMode.HTML)
      except Exception:
        pass

    asyncio.create_task(upload_and_edit_caption(sent_photo, image_bytes, review_url))
    return

  if data.startswith("search_"):
    pid = data.split("_")[1]
    context.user_data["awaiting_author_search"] = True
    context.user_data["search_pid"] = pid

    prompt_msg = await query.message.reply_text(
        "🔎 <b>Send the Author Name</b> to query local DB:",
        parse_mode=ParseMode.HTML,
    )
    context.user_data["prompt_msg_id"] = prompt_msg.message_id
    return

  if data.startswith("sync_"):
    pid = data.split("_")[1]
    status_msg = await query.message.reply_text(
        "🔄 <i>Fetching fresh reviews from Flipkart to update local DB...</i>",
        parse_mode=ParseMode.HTML,
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, crawl_and_cache_product, pid, 5, "recent")
    try:
      await status_msg.delete()
    except Exception:
      pass

    reviews = get_cached_reviews(pid, page=1, per_page=5)
    total_count = count_cached_reviews(pid)
    product_title = context.user_data.get("product_title", f"Product ({pid})")
    converted_info = context.user_data.get("converted_info", "")
    msg_text, reply_markup = build_review_message(
        reviews,
        pid,
        product_title,
        1,
        "recent",
        per_page=5,
        converted_info=converted_info,
        total_cached=total_count,
    )
    await query.edit_message_text(
        msg_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return

  data_parts = data.split("_")
  if len(data_parts) != 4 or data_parts[0] != "rev":
    return

  pid = data_parts[1]
  page = int(data_parts[2])
  sort_type = data_parts[3]
  product_title = context.user_data.get("product_title", f"Product ({pid})")
  converted_info = context.user_data.get("converted_info", "")

  reviews = get_cached_reviews(pid, page=page, per_page=5)

  if not reviews:
    loop = asyncio.get_running_loop()
    web_revs, _, _ = await loop.run_in_executor(None, fetch_flipkart_reviews, pid, page, sort_type)
    if web_revs:
      save_reviews_to_db(pid, product_title, web_revs, sort_type)
      reviews = get_cached_reviews(pid, page=page, per_page=5)

    if not reviews:
      await query.message.reply_text(f"⚠️ No more reviews on page {page} with current filters.")
      return

  total_count = count_cached_reviews(pid)
  links_dict = context.user_data.setdefault("review_links", {})
  for r in reviews:
    r_id = r.get("review_id") or r.get("reviewId")
    links_dict[r_id] = r["link"]

  msg_text, reply_markup = build_review_message(
      reviews,
      pid,
      product_title,
      page,
      sort_type,
      per_page=5,
      converted_info=converted_info,
      total_cached=total_count,
  )

  await query.edit_message_text(
      msg_text,
      reply_markup=reply_markup,
      parse_mode=ParseMode.HTML,
      disable_web_page_preview=True,
  )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
  start_time = time.time()
  status_msg = await update.message.reply_text("⏳ <b>Processing media...</b>", parse_mode=ParseMode.HTML)

  unique_name = ""
  dl_attempt = 1

  while True:
    try:
      if update.message.photo:
        file_obj = await asyncio.wait_for(update.message.photo[-1].get_file(), timeout=15)
        ext = ".jpg"
        size = update.message.photo[-1].file_size / 1024
      elif update.message.document:
        file_obj = await asyncio.wait_for(update.message.document.get_file(), timeout=15)
        ext = os.path.splitext(update.message.document.file_name)[1]
        size = update.message.document.file_size / 1024
      else:
        return

      if not unique_name:
        unique_name = f"{uuid.uuid4().hex[:8]}{ext}"

      await asyncio.wait_for(file_obj.download_to_drive(unique_name), timeout=25)
      break
    except Exception as e:
      print(f"⚠️ Telegram Download Attempt {dl_attempt} failed: {str(e)}")
      dl_attempt += 1
      try:
        await context.bot.edit_message_text(
            chat_id=update.message.chat_id,
            message_id=status_msg.message_id,
            text=f"🔄 <b>Retrying download from Telegram... ({dl_attempt})</b>",
            parse_mode=ParseMode.HTML,
        )
      except Exception:
        pass
      await asyncio.sleep(1.5)

  try:
    await context.bot.edit_message_text(
        chat_id=update.message.chat_id,
        message_id=status_msg.message_id,
        text="⏳ <b>Uploading to Drive...</b>",
        parse_mode=ParseMode.HTML,
    )
  except Exception:
    pass

  asyncio.create_task(
      upload_worker(
          unique_name,
          unique_name,
          update.message.chat_id,
          update.message.message_id,
          context,
          start_time,
          size,
          status_msg.message_id,
      )
  )


# ==========================================
# APP STARTUP
# ==========================================
if __name__ == "__main__":
  init_db()

  app = (
      ApplicationBuilder()
      .token(BOT_TOKEN)
      .connect_timeout(30.0)
      .read_timeout(30.0)
      .write_timeout(30.0)
      .pool_timeout(30.0)
      .build()
  )

  app.add_handler(CommandHandler("start", start))
  app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_messages))
  app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_media))
  app.add_handler(CallbackQueryHandler(button_callback))

  print("🚀 Bot Active (SQLite Local Cache + Instant DB Search)")
  if __name__ == "__main__":
    # ...
    app.run_polling(drop_pending_updates=True)
      
