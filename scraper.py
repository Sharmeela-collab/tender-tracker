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

NEW_HEADERS = [
    "Date Found", 
    "State", 
    "Organisation", 
    "Tender ID", 
    "Tender Title", 
    "Tender Category", 
    "Pre-bid Meeting Date", 
    "Bid Submission Date", 
    "Portal Link"
]

def get_sheet():
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
    
    sh = gc.open(SHEET_NAME).sheet1
    rows = sh.get_all_values()
    if len(rows) == 0 or rows[0] != NEW_HEADERS:
        sh.clear()
        sh.append_row(NEW_HEADERS)
    return sh

def save_to_sheet(sheet, tenders):
    if not tenders:
        print("\n--> No tenders to save today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Tender ID is Column D (index 3)
        existing_ids = {row[3].strip() for row in existing_records[1:] if len(row) > 3}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        t_id = item["tender_id"].strip()
        if t_id and t_id not in existing_ids and "Captcha" not in t_id:
            existing_ids.add(t_id)
            new_rows.append([
                today,
                item["state"],
                item["organisation"],
                t_id,
                item["title"],
                item["category"],
                item["pre_bid_date"],
                item["submission_date"],
                item["link"]
            ])

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"\n=======================================================")
        print(f" SUCCESS: SAVED {len(new_rows)} NEW TENDERS TO GOOGLE SHEET!")
        print(f"=======================================================")
    else:
        print("\nAll tenders captured already exist in Google Sheet.")

PORTALS = {
    "Maharashtra": "https://mahatenders.gov.in/nicgep/app",
    "Goa": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh": "https://mptenders.gov.in/nicgep/app",
    "Central CPPP": "https://eprocure.gov.in/eprocure/app"
}

DATE_REGEX = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}(?:\s+\d{1,2}:\d{2}\s+(?:AM|PM))?\b", re.IGNORECASE)

async def solve_captcha(page, ocr):
    captcha_el = await page.wait_for_selector("#captchaImage", timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return re.sub(r"[^a-zA-Z0-9]", "", solved.strip())

async def scrape_portal(portal_label, base_url, ocr, context):
    print(f"\n--------------------------------------------------")
    print(f"Opening: {portal_label}")
    print(f"--------------------------------------------------")
    
    page = await context.new_page()
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
    
    results = []

    try:
        search_url = f"{base_url}?page=FrontEndLatestActiveTenders&service=page"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # 1. CAPTCHA Handling Loop
        for attempt in range(5):
            captcha_input = await page.query_selector("#captchaText")
            if not captcha_input or not await captcha_input.is_visible():
                print(f"[{portal_label}] No CAPTCHA required or already solved!")
                break

            code = await solve_captcha(page, ocr)
            print(f"[{portal_label}] Attempt {attempt + 1}: Submitting '{code}'")

            await captcha_input.fill("")
            await captcha_input.type(code, delay=30)
            await asyncio.sleep(1)

            submitted = False
            submit_buttons = await page.query_selector_all("input[type='submit'], input[name='Submit'], #Submit")
            for btn in submit_buttons:
                val = (await btn.get_attribute("value") or "").lower()
                name = (await btn.get_attribute("name") or "").lower()
                if "submit" in val or "submit" in name:
                    await btn.click()
                    submitted = True
                    break

            if not submitted:
                await page.evaluate("""() => {
                    const form = document.querySelector('form');
                    if (form) form.submit();
                }""")

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
