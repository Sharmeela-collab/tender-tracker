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
        print("\n--> No tenders extracted from portals today.")
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
        print(f" SUCCESS: WROTE {len(new_rows)} NEW TENDERS TO GOOGLE SHEET!")
        print(f"=======================================================")
        for r in new_rows:
            print(f" [+] [{r[1]}] Dept: {r[2][:35]} | ID: {r[3]} | Due: {r[7]}")
            print(f"     Title: {r[4][:70]}...\n")
    else:
        print("\nAll captured tenders already exist in your Google Sheet.")

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

        # 1. Handle CAPTCHA if presented
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

        # 2. Extract Data from Results Table
        try:
            await page.wait_for_selector("#table, table.list_table", timeout=10000)
        except Exception:
            pass

        # Target all table rows inside the main results table
        rows = await page.query_selector_all("#table tr, table.list_table tr")
        print(f"[{portal_label}] Inspecting {len(rows)} table rows...")

        for row in rows:
            cells = await row.query_selector_all("td")
            
            # Real GePNIC active tender rows have at least 4 or 5 columns
            if len(cells) < 4:
                continue

            row_text = (await row.inner_text()).strip()

            # Ignore header rows, sorting controls, or empty rows
            if not row_text or "Tender Title" in row_text or "S.No" in row_text or "Select Sorting" in row_text:
                continue

            # Must contain at least one date
            dates_in_row = DATE_REGEX.findall(row_text)
            if not dates_in_row:
                continue

            # Exact GePNIC Column Mapping:
            # cells[0]: S.No
            # cells[1]: e-Published Date
            # cells[2]: Bid Submission Closing Date
            # cells[3]: Tender Opening Date
            # cells[4]: Tender Title and Ref No / ID
            # cells[5] (if present): Organisation Chain
            
            # Submission Date:
            submission_date = (await cells[2].inner_text()).strip()
            if not DATE_REGEX.search(submission_date):
                submission_date = dates_in_row[0] if dates_in_row else "Check Portal"

            # Title and Link (Cell 4 if available, otherwise locate anchor)
            link_el = await row.query_selector("a[id*='DirectLink'], a[href*='TenderDetails'], a")
            title = ""
            href = ""
            if link_el:
                title = (await link_el.inner_text()).strip()
                href = await link_el.get_attribute("href") or ""

            # Fallback if cell 4 contains text
            if len(cells) >= 5 and (not title or len(title) < 5):
                cell4_text = (await cells[4].inner_text()).strip()
                title = cell4_text.split("\n")[0]

            title = re.sub(r"\s+", " ", title).strip()

            # If title is still empty or is just UI noise, skip
            if not title or len(title) < 5 or "Select Sorting" in title:
                continue

            # Tender ID extraction:
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", row_text)
            if id_match:
                tender_id = id_match.group(0)
            else:
                # Look for Ref No text inside cell 4
                ref_match = re.search(r"\[([^\]]+)\]", row_text)
                if ref_match:
                    tender_id = ref_match.group(1).strip()
                else:
                    tender_id = re.sub(r"[^\w\-_/]", "", title[:25])

            # Organisation:
            # In GePNIC, Organisation Chain is either in Cell 5 or specified in the title block
            org = "State Authority / ULB"
            if len(cells) >= 6:
                org_text = (await cells[5].inner_text()).strip()
                if len(org_text) > 3 and not DATE_REGEX.search(org_text):
                    # Clean up long chains like 'Dept||Zone||Division' to first two elements
                    parts = [p.strip() for p in org_text.split("||") if p.strip()]
                    org = " - ".join(parts[:2]) if parts else org_text
            elif len(cells) >= 5:
                # Sometimes org is cell 1 or cell 4 secondary line
                lines = [l.strip() for l in row_text.split("\n") if len(l.strip()) > 3]
                for l in lines:
                    if l != title and not DATE_REGEX.search(l) and "S.No" not in l and len(l) > 5:
                        org = l
                        break

            # Pre-bid date
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
                "title": title,
                "category": category,
                "pre_bid_date": pre_bid_date,
                "submission_date": submission_date,
                "link": full_link
            })

        print(f"[{portal_label}] Clean tenders extracted: {len(results)}")

    except Exception as e:
        print(f"[{portal_label}] Crawl notice: {e}")
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
