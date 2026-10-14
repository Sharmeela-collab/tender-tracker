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

PORTALS = {
    "Maharashtra": {
        "base_url": "https://mahatenders.gov.in/nicgep/app",
        "search_url": "https://mahatenders.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
    },
    "Goa": {
        "base_url": "https://eprocure.goa.gov.in/nicgep/app",
        "search_url": "https://eprocure.goa.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
    },
    "Madhya Pradesh": {
        "base_url": "https://mptenders.gov.in/nicgep/app",
        "search_url": "https://mptenders.gov.in/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
    },
    "Central CPPP": {
        "base_url": "https://eprocure.gov.in/eprocure/app",
        "search_url": "https://eprocure.gov.in/eprocure/app?page=FrontEndLatestActiveTenders&service=page"
    }
}

DATE_REGEX = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}(?:\s+\d{1,2}:\d{2}\s+(?:AM|PM))?\b", re.IGNORECASE)

UI_EXCLUDE_TERMS = [
    "select sorting", "sorting option", "published date", "closing date",
    "opening date", "screen reader", "search tenders", "advanced search",
    "corrigendum", "tender status", "captcha", "s.no", "tender title", "clear"
]

def is_noise(text: str) -> bool:
    if not text or len(text.strip()) < 5:
        return True
    clean = text.lower().strip()
    return any(term in clean for term in UI_EXCLUDE_TERMS)

def get_sheet():
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        raise ValueError("Error: GCP_CREDENTIALS environment variable missing.")
    
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
        print("\n--> No fresh tenders found today.")
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
        if t_id and t_id not in existing_ids:
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
        for r in new_rows:
            print(f" [+] [{r[1]}] Dept: {r[2][:30]} | ID: {r[3]}")
            print(f"     Title: {r[4][:60]}...")
            print(f"     Link:  {r[8]}\n")
    else:
        print("\nAll extracted tenders already exist in your Google Sheet.")

async def solve_captcha(page, ocr):
    captcha_el = await page.wait_for_selector("#captchaImage", timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return re.sub(r"[^a-zA-Z0-9]", "", solved.strip())

async def scrape_portal(portal_label, portal_cfg, ocr, context):
    base_url = portal_cfg["base_url"]
    search_url = portal_cfg["search_url"]

    print(f"\n--------------------------------------------------")
    print(f"Connecting to: {portal_label}")
    print(f"--------------------------------------------------")
    
    page = await context.new_page()
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
    
    results = []

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # 1. CAPTCHA Handling
        for attempt in range(5):
            captcha_input = await page.query_selector("#captchaText")
            if not captcha_input or not await captcha_input.is_visible():
                break

            code = await solve_captcha(page, ocr)
            print(f"[{portal_label}] Attempt {attempt + 1}: Submitting '{code}'")

            await captcha_input.fill("")
            await captcha_input.type(code, delay=30)
            await asyncio.sleep(1)

            submit_buttons = await page.query_selector_all("input[type='submit'], input[name='Submit'], #Submit")
            submitted = False
            for btn in submit_buttons:
                val = (await btn.get_attribute("value") or "").lower()
                name = (await btn.get_attribute("name") or "").lower()
                if "submit" in val or "submit" in name:
                    try:
                        await btn.click(timeout=5000, no_wait_after=True)
                        submitted = True
                        break
                    except Exception:
                        pass

            if not submitted:
                await page.evaluate("() => { const f = document.querySelector('form'); if(f) f.submit(); }")

            await asyncio.sleep(3)

            if not await page.query_selector("#captchaText"):
                print(f"[{portal_label}] CAPTCHA bypassed successfully!")
                break
            else:
                refresh_btn = await page.query_selector("#Image1, a[title*='Refresh']")
                if refresh_btn:
                    await refresh_btn.click()
                    await asyncio.sleep(2)

        # 2. Extract Data from Rows
        try:
            await page.wait_for_selector("#table, table.list_table", timeout=10000)
        except Exception:
            pass

        rows = await page.query_selector_all("#table tr, table.list_table tr")
        print(f"[{portal_label}] Found {len(rows)} table rows...")

        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            row_raw_text = (await row.inner_text()).strip()
            if is_noise(row_raw_text):
                continue

            dates = DATE_REGEX.findall(row_raw_text)
            if not dates:
                continue

            # Submission closing date
            submission_date = dates[1] if len(dates) > 1 else dates[0]

            # Title extraction
            link_el = await row.query_selector("a[id*='DirectLink'], a[href*='TenderDetails'], a")
            title = ""
            raw_href = ""
            if link_el:
                title = (await link_el.inner_text()).strip()
                raw_href = await link_el.get_attribute("href") or ""

            if (not title or len(title) < 8) and len(cells) >= 5:
                cell4_text = (await cells[4].inner_text()).strip()
                lines = [l.strip() for l in cell4_text.split("\n") if l.strip()]
                if lines:
                    title = lines[0]

            title = re.sub(r"\s+", " ", title).strip()
            if is_noise(title):
                continue

            # ID extraction
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", row_raw_text)
            if id_match:
                tender_id = id_match.group(0)
            else:
                bracket_match = re.search(r"\[([^\]]+)\]", row_raw_text)
                if bracket_match:
                    tender_id = bracket_match.group(1).strip()
                else:
                    clean_str = re.sub(r"[^A-Za-z0-9]", "", title[:15])
                    tender_id = f"{portal_label[:2]}_{clean_str}"

            # Department / Organisation
            org = f"{portal_label} Department"
            if len(cells) >= 6:
                dept_text = (await cells[5].inner_text()).strip()
                if dept_text and not DATE_REGEX.search(dept_text) and not is_noise(dept_text):
                    chain = [p.strip() for p in dept_text.split("||") if p.strip()]
                    org = " - ".join(chain[:2]) if chain else dept_text
            elif len(cells) >= 5:
                lines = [l.strip() for l in row_raw_text.split("\n") if len(l.strip()) > 3]
                for l in lines:
                    if l != title and not DATE_REGEX.search(l) and not is_noise(l) and len(l) > 4:
                        org = l
                        break

            # Category
            category = "Works / Services"
            cat_match = re.search(r"\b(Services|Works|Goods|Consultancy)\b", row_raw_text, re.IGNORECASE)
            if cat_match:
                category = cat_match.group(0).capitalize()

            # Pre-bid meeting
            pre_bid_match = re.search(r"pre-?bid[^\n:]*[:\s]+([0-9A-Za-z\s:-]{8,25})", row_raw_text, re.IGNORECASE)
            pre_bid_date = pre_bid_match.group(1).strip() if pre_bid_match else "Refer Tender Document"

            # Construct Link
            if tender_id and "_" in tender_id:
                direct_link = f"{base_url}?page=FrontEndTenderDetails&service=page&tenderId={tender_id}"
            elif raw_href.startswith("?"):
                direct_link = f"{base_url}{raw_href}"
            elif raw_href.startswith("http"):
                direct_link = raw_href
            else:
                direct_link = search_url

            results.append({
                "state": portal_label,
                "organisation": org,
                "tender_id": tender_id,
                "title": title,
                "category": category,
                "pre_bid_date": pre_bid_date,
                "submission_date": submission_date,
                "link": direct_link
            })

        print(f"[{portal_label}] Clean tenders extracted: {len(results)}")

    except Exception as e:
        print(f"[{portal_label}] Fetch notice: {e}")
    finally:
        await page.close()

    return results

async def main():
    sheet = get_sheet()
    ocr = ddddocr.DdddOcr(show_ad=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            locale="en-IN",
            timezone_id="Asia/Kolkata"
        )

        all_tenders = []
        for label, cfg in PORTALS.items():
            tenders = await scrape_portal(label, cfg, ocr, context)
            all_tenders.extend(tenders)

        await browser.close()

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())
