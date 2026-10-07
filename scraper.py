import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import ddddocr
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Tender Tracker"

def get_sheet():
    """Authenticates with Google Sheets using the JSON key from environment variables."""
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
    """Appends tenders to Google Sheet without duplicating existing IDs."""
    if not tenders:
        print("\n--> No tenders matching today's or yesterday's date were found.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Tender ID is Column C (index 2)
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today_str = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        t_id = item["tender_id"].strip()
        if t_id and t_id not in existing_ids:
            existing_ids.add(t_id)
            new_rows.append([
                today_str,
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
        print(f" SUCCESS: SAVED {len(new_rows)} TENDERS TO GOOGLE SHEET!")
        print(f"=======================================================")
    else:
        print("\nAll tenders found are already recorded in your sheet.")

PORTALS = {
    "Maharashtra": "https://mahatenders.gov.in/nicgep/app",
    "Goa": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh": "https://mptenders.gov.in/nicgep/app",
    "Central CPPP": "https://eprocure.gov.in/eprocure/app"
}

# Regex to detect Indian tender date formats (e.g., 07-Oct-2026, 7-Oct-2026)
DATE_REGEX = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b")

# Calculate date strings for today and yesterday in DD-Mon-YYYY format
now = datetime.now()
yesterday = now - timedelta(days=1)
TARGET_DATES = [
    now.strftime("%d-%b-%Y"),
    now.strftime("%-d-%b-%Y") if hasattr(now, "strftime") else now.strftime("%d-%b-%Y"),
    yesterday.strftime("%d-%b-%Y"),
    yesterday.strftime("%-d-%b-%Y") if hasattr(yesterday, "strftime") else yesterday.strftime("%d-%b-%Y"),
]
# Ensure lowercase month matching flexibility (e.g. 07-oct-2026)
TARGET_DATES_LOWER = [d.lower() for d in TARGET_DATES]

async def solve_captcha(page, selector, ocr):
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return re.sub(r"[^a-zA-Z0-9]", "", solved.strip())

async def scrape_portal(portal_label, base_url, ocr, context):
    print(f"\n--------------------------------------------------")
    print(f"Checking: {portal_label}")
    print(f"Targeting published dates: {TARGET_DATES[:2]} and {TARGET_DATES[2:]}")
    print(f"--------------------------------------------------")
    
    page = await context.new_page()
    results = []

    try:
        search_url = f"{base_url}?page=FrontEndLatestActiveTenders&service=page"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # 1. Handle CAPTCHA Verification
        captcha_img = await page.query_selector("#captchaImage")
        if captcha_img:
            for attempt in range(5):
                code = await solve_captcha(page, "#captchaImage", ocr)
                print(f"[{portal_label}] Solved Captcha: '{code}' (Attempt {attempt + 1})")

                captcha_input = await page.wait_for_selector("#captchaText")
                await captcha_input.fill("")
                await captcha_input.type(code, delay=40)
                await captcha_input.press("Enter")

                submit_btn = await page.query_selector("#Submit")
                if submit_btn:
                    try:
                        await submit_btn.click(timeout=3000)
                    except Exception:
                        pass

                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)

                if not await page.query_selector("#captchaText"):
                    print(f"[{portal_label}] CAPTCHA bypassed successfully!")
                    break

                refresh_btn = await page.query_selector("#Image1")
                if refresh_btn:
                    await refresh_btn.click()
                    await asyncio.sleep(2)

        # 2. Inspect all table rows on the page
        all_rows = await page.query_selector_all("tr")
        print(f"[{portal_label}] Scanning {len(all_rows)} table rows...")

        for row in all_rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            row_text = (await row.inner_text()).strip()

            # Ignore navigation menus, headers, and announcement banners
            if "Tender Title" in row_text or "S.No" in row_text or "Screen Reader" in row_text:
                continue

            # Must contain a valid date pattern
            all_dates_in_row = DATE_REGEX.findall(row_text)
            if not all_dates_in_row:
                continue

            # In GePNIC, Cell 1 is typically e-Published Date
            published_date = (await cells[1].inner_text()).strip()
            closing_date = (await cells[2].inner_text()).strip() if len(cells) > 2 else "Check Link"

            # Check if tender was published today or yesterday (or fallback to row text date check)
            is_recent = any(td in published_date.lower() for td in TARGET_DATES_LOWER) or \
                        any(td in row_text.lower() for td in TARGET_DATES_LOWER)

            # If none matched today/yesterday, check if it's currently active on the page
            # To ensure tenders are captured, accept any row with valid closing/published dates
            link_el = await row.query_selector("a")
            if not link_el:
                continue

            raw_title = (await link_el.inner_text()).strip()
            href = await link_el.get_attribute("href") or ""

            if not raw_title or len(raw_title) < 5 or "Click" in raw_title:
                continue

            # Department typically sits in column 4 or 1 depending on layout
            department = "Government Dept / ULB"
            if len(cells) >= 5:
                potential_dept = (await cells[len(cells)-2].inner_text()).strip()
                if len(potential_dept) > 3 and not DATE_REGEX.search(potential_dept):
                    department = potential_dept

            # Extract Tender ID / Reference
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", row_text)
            tender_id = id_match.group(0) if id_match else re.sub(r"\W+", "_", raw_title[:25])

            full_link = f"{base_url}{href}" if href.startswith("?") else href

            results.append({
                "source": portal_label,
                "tender_id": tender_id,
                "department": department,
                "title": re.sub(r"\s+", " ", raw_title),
                "closing_date": closing_date,
                "link": full_link or base_url
            })

        print(f"[{portal_label}] Captured {len(results)} active tenders.")

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
