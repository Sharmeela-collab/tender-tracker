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
        print("\n--> No new genuine tenders found today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Column D (index 3) is Tender ID
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
        print(f" SUCCESS: SAVED {len(new_rows)} GENUINE TENDERS TO GOOGLE SHEET!")
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

# Words that indicate a row is a sorting tool, filter, or UI button, NOT a tender
UI_NOISE_PATTERN = re.compile(
    r"select\s+sorting|sorting\s+option|tender\s+id\s*$|published\s+date\s*$|"
    r"closing\s+date\s*$|opening\s+date\s*$|screen\s+reader|search\s+tenders|"
    r"advanced\s+search|corrigendum|results\s+of\s+tenders|tender\s+status|"
    r"clear\s*$|back\s*$|submit\s*$|captcha",
    re.IGNORECASE
)

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

        # 1. Solve CAPTCHA if prompted
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
                    await btn.click()
                    submitted = True
                    break

            if not submitted:
                await page.evaluate("() => { const form = document.querySelector('form'); if (form) form.submit(); }")

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(3)

            if not await page.query_selector("#captchaText"):
                print(f"[{portal_label}] Captcha solved successfully!")
                break
            else:
                refresh_btn = await page.query_selector("#Image1, a[title*='Refresh']")
                if refresh_btn:
                    await refresh_btn.click()
                    await asyncio.sleep(2)

        # 2. Find ONLY links that lead to actual tender details
        # GePNIC tender title links have href with 'FrontEndTenderDetails' or 'tender' or 'direct=1'
        tender_links = await page.query_selector_all(
            "a[href*='TenderDetails'], a[href*='page=FrontEndTenderDetails'], a[href*='direct=1'], a[id*='DirectLink']"
        )
        print(f"[{portal_label}] Genuine tender links found: {len(tender_links)}")

        for link in tender_links:
            title = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""

            # Discard any empty text or UI artifacts
            if not title or len(title) < 10 or UI_NOISE_PATTERN.search(title):
                continue

            # Get parent row to extract dates, department, and ID
            row = await link.evaluate_handle("el => el.closest('tr')")
            row_text = (await row.inner_text()).strip() if row else ""
            cells = await row.query_selector_all("td") if row else []

            # Extract dates
            found_dates = DATE_REGEX.findall(row_text)
            submission_date = "Check Link"
            if len(found_dates) >= 2:
                submission_date = found_dates[1]
            elif len(found_dates) == 1:
                submission_date = found_dates[0]

            # Extract Organisation Name
            org = "State Authority / ULB"
            if len(cells) >= 4:
                # Column 1 or 2 often has the Dept
                for c in cells:
                    c_txt = (await c.inner_text()).strip()
                    if len(c_txt) > 3 and not DATE_REGEX.search(c_txt) and c_txt != title and not UI_NOISE_PATTERN.search(c_txt):
                        org = c_txt
                        break

            # Extract Tender ID
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b[0-9]{6,}\b)", row_text)
            if id_match:
                tender_id = id_match.group(0)
            else:
                tender_id = re.sub(r"[^\w\-_/]", "", title[:25])

            if UI_NOISE_PATTERN.search(tender_id):
                tender_id = f"TND_{len(results)+1}"

            # Pre-bid meeting date
            pre_bid_match = re.search(r"pre-?bid[^\n:]*[:\s]+([0-9A-Za-z\s:-]{8,25})", row_text, re.IGNORECASE)
            pre_bid_date = pre_bid_match.group(1).strip() if pre_bid_match else "See RFP Document"

            # Category
            category = "Works / Services"
            cat_match = re.search(r"\b(Services|Works|Goods|Consultancy)\b", row_text, re.IGNORECASE)
            if cat_match:
                category = cat_match.group(0).capitalize()

            full_link = f"{base_url}{href}" if href.startswith("?") else (href or base_url)

            results.append({
                "state": portal_label,
                "organisation": org,
                "tender_id": tender_id,
                "title": re.sub(r"\s+", " ", title),
                "category": category,
                "pre_bid_date": pre_bid_date,
                "submission_date": submission_date,
                "link": full_link
            })

        print(f"[{portal_label}] Clean tenders extracted: {len(results)}")

    except Exception as e:
        print(f"[{portal_label}] Notice: {e}")
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
        for label, url in PORTALS.items():
            tenders = await scrape_portal(label, url, ocr, context)
            all_tenders.extend(tenders)

        await browser.close()

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())
