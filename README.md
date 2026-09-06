# Flipkart Review Scraper & Drive Uploader Telegram Bot 🚀

A multi-purpose Python Telegram bot designed to fetch, paginate, and search product reviews from Flipkart and Shopsy with local SQLite caching. It also features automatic image compression and background uploads to Google Drive via Rclone, as well as desktop review screenshot generation.

---

## ⚡ Features

- **Review Extraction & Pagination:** Parses product reviews from Flipkart/Shopsy with support for multiple sorting orders (Latest, Helpful, Positive, Negative).
- **SQLite Database Caching:** Automatically saves fetched reviews to a local SQLite database (`reviews_cache.db`) for fast pagination and offline browsing.
- **Author & Content Search:** Query specific reviewer names, review titles, or text directly from cached product data.
- **Desktop Screenshot Engine:** Renders headless browser screenshots of individual reviews using Chromium (with Microlink API fallback).
- **Automated Media Processing:** Compresses incoming images/documents to a targeted size (default: 50 KB) and uploads them directly to Google Drive via Rclone.
- **Link Transformation:** Automatically generates direct links to switch between Flipkart and Shopsy product pages, including "Write Review" entrypoints.

---

## 📋 Prerequisites

- **Python:** v3.10 or higher
- **Rclone:** Installed and pre-configured with a remote drive (e.g., named `gdrive`).
- **Chromium (Optional):** Headless browser installed on Termux or your Linux environment for local screenshots:
  ```bash
  pkg install chromium
  ```
**## 🚀 Setup & Installation**
1. Clone the repository:
 ```bash
 git clone https://github.com/Debugcodec/fk-reviews-tg-bot-aio.git
```
2. Install Python dependencies:
```bash
 pip install -r requirements.txt
```
3. Set up environment variables:
```bash
 cp .env.example .env
```
## ⚙️ Environment Variables

Configure your `.env` file with your bot credentials and cloud drive configurations:

| Variable | Required | Default | Description | Example |
| :--- | :---: | :---: | :--- | :--- |
| `BOT_TOKEN` | Yes | — | Telegram Bot token obtained from [@BotFather](https://t.me/BotFather). | `1854272732:AA...` |
| `REMOTE_NAME` | No | `gdrive` | Rclone remote drive identifier. | `gdrive` |
| `REMOTE_FOLDER` | No | `TelegramBot` | Target folder name in your cloud drive. | `TelegramBot` |
| `REMOTE_FOLDER_ID`| Yes | — | Public/shared Google Drive folder ID for generated direct links. | `1DxRYclliOO...` |
| `TARGET_KB` | No | `50` | Maximum target size in KB for automatic image compression. | `50` |

> ⚠️ **Security Warning:** Never commit `.env` or any `.db` files to GitHub. Ensure both are listed in `.gitignore`.

 Running the Bot
Standard Run
Run the script directly from your terminal:
```bash
 python bot.py

```
## 📖 Usage Guide

* **Fetch Reviews:** Send any Flipkart or Shopsy product URL into the chat. The bot automatically extracts the Product ID (`pid`), checks the local SQLite cache, and crawls online reviews if not yet saved.
* **Filter Reviews:** Use the inline buttons below the review cards to sort by **Latest**, **Helpful**, **Positive**, or **Negative**.
* **Search Authors:** Tap **🔍 Search Author** and send a reviewer's name or keyword to query matching entries directly from the local database.
* **Capture Screenshots:** Tap **📸 Screenshot #X** next to any review to generate a full desktop snapshot and receive a direct Google Drive link.
* **Direct Drive Upload:** Send any photo or image document to the bot. It compresses the file to the configured `TARGET_KB` and uploads it directly to your connected Google Drive folder.


