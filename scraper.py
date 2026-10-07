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
    if len(sh.get_all_values()) == 0:
        sh.append_row([
            "Date Found", "State / Source", "Tender ID", 
            "Department / Org", "Work Description / Title", 
            "Closing Date", "Tender Link"
        ])
    return sh

def save_to_sheet(sheet, tenders):
    if not tenders:
        print("\n--> No valid tenders to append today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        t_id = item["tender_id"].strip()
        # Ensure it's not a garbage row or UI artifact
        if t_id and t_id not in existing_ids and "Captcha" not in t_id:
            existing_ids.add(t_id)
            new_rows.append([
                today,
                item["source"],
                t_id,
                item["department"],
                item["title"],
                item["closing_date"],
                item["link"]
            ])

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"\n=======================================================")
        print(f" SUCCESS: SAVED {len(new_rows)} REAL TENDERS TO GOOGLE SHEET!")
        print(f"=======================================================")
    else:
        print("\nNo new unique tenders to add.")

PORTALS = {
    "Maharashtra": "https://mahatenders.gov.in/nicgep/app",
    "Goa": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh": "https://mptenders.gov.in/nicgep/app",
    "Central CPPP": "https://eprocure.gov.in/eprocure/app"
}

# Negative filter to completely ignore form UI rows
GARBAGE_PATTERNS = re.compile(
    r"enter captcha|captcha text|refresh|search|s\.no|tender title|bid opening date|"
    r"click here to view|advanced search|tender search", 
    re.IGNORECASE
)

async def solve_captcha(page, selector, ocr):
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return re.sub(r"[^a-zA-Z0-9]", "", solved.strip())

async def scrape_portal(portal_label, base_url, ocr, context):
    print(f"\n--------------------------------------------------")
    print(f"Opening: {portal_label}")
    print(f"--------------------------------------------------")
    
    page = await context.new_page()
    results = []

    try:
        search_url = f"{base_url}?page=FrontEndLatestActiveTenders&service=page"
        await page.goto(search_url, wait_until="networkidle", timeout=45000)
        await asyncio.sleep(2)

        # 1. Handle CAPTCHA Submission
        captcha_img = await page.query_selector("#captchaImage")
        if captcha_img:
            for attempt in range(5):
                code = await solve_captcha(page, "#captchaImage", ocr)
                print(f"[{portal_label}] Attempt {attempt + 1}: Solved as '{code}'")

                captcha_input = await page.wait_for_selector("#captchaText")
                await captcha_input.fill("")
                await captcha_input.type(code, delay=50)

                # Press Enter key inside the field to trigger form submission reliably
                await captcha_input.press("Enter")
                
                # Also click Submit button if present
                submit_btn = await page.query_selector("#Submit")
                if submit_btn:
                    try:
                        await submit_btn.click(timeout=3000)
                    except Exception:
                        pass

                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)

                # Check if captcha form is gone (success)
                still_has_captcha = await page.query_selector("#captchaText")
                if not still_has_captcha:
                    print(f"[{portal_label}] CAPTCHA bypassed successfully!")
                    break
                
                # If still present, click refresh icon and retry
                print(f"[{portal_label}] CAPTCHA was incorrect, refreshing...")
                refresh_btn = await page.query_selector("#Image1")
                if refresh_btn:
                    await refresh_btn.click()
                    await asyncio.sleep(2)

        # 2. Extract ONLY valid tender link anchors
        # In GePNIC, tender rows always contain an anchor linking to the tender's view page
        anchor_elements = await page.query_selector_all("table a[href*='page='], table a[href*='service=page']")
        print(f"[{portal_label}] Found {len(anchor_elements)} clickable tender links.")

        for a in anchor_elements:
            raw_text = (await a.inner_text()).strip()
            href = await a.get_attribute("href") or ""

            # Skip header links, navigation links, and captcha elements
            if not raw_text or GARBAGE_PATTERNS.search(raw_text) or len(raw_text) < 5:
                continue

            # Find parent table row to get metadata (closing date, department, ID)
            row = await a.evaluate_handle("el => el.closest('tr')")
            cells = await row.query_selector_all("td") if row else []
            
            closing_date = "Check Link"
            department = "State Authority"
            raw_id = ""

            if len(cells) >= 3:
                # In GePNIC Active Tenders:
                # Cell 0: S.No
                # Cell 1: e-Published Date
                # Cell 2: Closing Date
                # Cell 3: Opening Date
                # Cell 4: Title and Ref No
                closing_date = (await cells[2].inner_text()).strip()
                if len(cells) >= 5:
                    department = (await cells[1].inner_text()).strip()
                raw_id = (await cells[len(cells) - 1].inner_text()).strip()

            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", raw_id + " " + raw_text)
            tender_id = id_match.group(0) if id_match else re.sub(r"\W+", "_", raw_text[:25])

            full_link = f"{base_url}{href}" if href.startswith("?") else href

            results.append({
                "source": portal_label,
                "tender_id": tender_id,
                "department": department,
                "title": re.sub(r"\s+", " ", raw_text),
                "closing_date": closing_date,
                "link": full_link or base_url
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
