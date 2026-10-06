import asyncio
import json
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright
import ddddocr
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Tender Tracker"

def get_sheet():
    """Connects to your Google Sheet using the secret key."""
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        raise ValueError("Error: GCP_CREDENTIALS not found in environment.")
    
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    return gc.open(SHEET_NAME).sheet1

def save_to_sheet(sheet, tenders):
    """Adds found tenders to Google Sheet without duplicating existing ones."""
    if not tenders:
        print("No matching tenders found today.")
        return

    # Read existing IDs in the sheet so we don't paste duplicates
    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Tender ID is in Column C (index 2)
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        if item["tender_id"] not in existing_ids:
            new_rows.append([
                today,
                item["portal"],
                item["tender_id"],
                item["title"],
                item["closing_date"],
                item["link"]
            ])

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"Success: Added {len(new_rows)} new tenders to your Google Sheet!")
    else:
        print("No new tenders found (all matches were already in the sheet).")

# Portals to scrape
PORTALS = {
    "Maharashtra": "https://mahatenders.gov.in/nicgep/app",
    "Goa": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh": "https://mptenders.gov.in/nicgep/app"
}

# Targeted keywords
KEYWORDS = [
    r"\bpmc\b",
    r"project management consult",
    r"feasibility",
    r"\bdpr\b",
    r"urban mobility",
    r"transit",
    r"metro",
    r"brts",
    r"bus terminal",
    r"authority engineer",
    r"independent engineer",
    r"highways?",
    r"expressway",
    r"multi.?modal",
    r"traffic survey"
]
KEYWORD_REGEX = re.compile("|".join(KEYWORDS), re.IGNORECASE)

async def solve_captcha(page, selector, ocr):
    """Takes a screenshot of the captcha box and uses ddddocr to decode text."""
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return solved.strip().replace(" ", "")

async def scrape_portal(portal_name, base_url, ocr):
    print(f"\nChecking {portal_name}...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        try:
            # Open the active tenders page
            search_url = f"{base_url}?page=WebActiveTenders&service=page"
            await page.goto(search_url, wait_until="networkidle", timeout=30000)

            # Check if CAPTCHA is present on page
            if await page.query_selector("#captchaImage"):
                for attempt in range(3):
                    code = await solve_captcha(page, "#captchaImage", ocr)
                    print(f"[{portal_name}] Captcha guess: {code}")
                    await page.fill("#captchaText", code)
                    await page.click("#Submit")
                    await page.wait_for_load_state("networkidle")
                    
                    if not await page.query_selector("text='Invalid Captcha'"):
                        break
                    
                    refresh_btn = await page.query_selector("#Image1")
                    if refresh_btn:
                        await refresh_btn.click()
                        await asyncio.sleep(1)

            # Find table rows with tenders
            rows = await page.query_selector_all("table.list_table tr")
            matches = []

            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                title = (await cells[1].inner_text()).strip()
                t_id = (await cells[2].inner_text()).strip()
                closing = (await cells[3].inner_text()).strip()

                # Filter using our transport / PMC keywords
                if KEYWORD_REGEX.search(title):
                    link_el = await cells[1].query_selector("a")
                    href = await link_el.get_attribute("href") if link_el else ""
                    full_link = f"{base_url}{href}" if href.startswith("?") else href

                    matches.append({
                        "portal": portal_name,
                        "tender_id": t_id,
                        "title": title.replace("\n", " "),
                        "closing_date": closing,
                        "link": full_link
                    })
            
            print(f"[{portal_name}] Found {len(matches)} matching tenders.")
            return matches
        except Exception as e:
            print(f"[{portal_name}] Notice: {e}")
            return []
        finally:
            await browser.close()

async def main():
    sheet = get_sheet()
    ocr = ddddocr.DdddOcr(show_ad=False)
    
    all_tenders = []
    for state, url in PORTALS.items():
        tenders = await scrape_portal(state, url, ocr)
        all_tenders.extend(tenders)

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())