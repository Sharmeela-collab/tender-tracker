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
    """Connects to Google Sheets using the GitHub Secret."""
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        raise ValueError("Error: GCP_CREDENTIALS not found in environment secrets.")
    
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
    """Appends all tenders without requiring any keyword match."""
    if not tenders:
        print("\n--> No tenders extracted from portals today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Tender ID is Column C (index 2)
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        t_id = item["tender_id"].strip()
        if t_id not in existing_ids:
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
        print(f" SUCCESS: WROTE {len(new_rows)} TENDERS TO YOUR GOOGLE SHEET!")
        print(f"=======================================================")
    else:
        print("\nAll tenders extracted are already present in your sheet.")

PORTALS = {
    "Maharashtra (MahaTenders)": "https://mahatenders.gov.in/nicgep/app",
    "Goa eProcure": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh Tenders": "https://mptenders.gov.in/nicgep/app",
    "Central CPPP": "https://eprocure.gov.in/eprocure/app"
}

async def solve_captcha(page, selector, ocr):
    """Takes a snapshot of the captcha and extracts characters."""
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return solved.strip().replace(" ", "")

async def scrape_all_tenders(portal_label, base_url, ocr, context):
    print(f"\n--------------------------------------------------")
    print(f"Fetching ALL tenders from: {portal_label}")
    print(f"--------------------------------------------------")
    
    page = await context.new_page()
    results = []

    try:
        # Load the latest active tenders endpoint
        search_url = f"{base_url}?page=FrontEndLatestActiveTenders&service=page"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2)

        # Solve Captcha if present
        captcha_img = await page.query_selector("#captchaImage")
        if captcha_img:
            for attempt in range(4):
                code = await solve_captcha(page, "#captchaImage", ocr)
                print(f"[{portal_label}] Captcha code: '{code}' (Attempt {attempt + 1})")
                
                await page.fill("#captchaText", code)
                submit_btn = await page.query_selector("#Submit")
                if submit_btn:
                    await submit_btn.click()

                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)

                err = await page.query_selector("text='Invalid Captcha'")
                if not err and not await page.query_selector("#captchaText"):
                    print(f"[{portal_label}] Captcha cleared successfully!")
                    break

                refresh = await page.query_selector("#Image1")
                if refresh:
                    await refresh.click()
                    await asyncio.sleep(1)

        # Parse every row found in the table (NO KEYWORD FILTER)
        rows = await page.query_selector_all("#table tr, table.list_table tr, table.table-bordered tr")
        print(f"[{portal_label}] Total rows detected: {len(rows)}")

        for row in rows:
            text = (await row.inner_text()).strip()
            # Skip header lines
            if not text or "Tender Title" in text or "S.No" in text:
                continue

            cells = await row.query_selector_all("td")
            if len(cells) < 3:
                continue

            link_el = await row.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else ""
            raw_title = (await link_el.inner_text()).strip() if link_el else text[:120]

            # Closing date
            closing_date = (await cells[2].inner_text()).strip() if len(cells) > 2 else "Check Link"

            # Department / Organisation
            department = "Government Dept / ULB"
            if len(cells) >= 5:
                department = (await cells[1].inner_text()).strip()

            # Tender ID
            raw_id = (await cells[len(cells) - 1].inner_text()).strip() if len(cells) > 3 else ""
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b\d{6,}\b)", raw_id + " " + text)
            tender_id = id_match.group(0) if id_match else re.sub(r"\W+", "_", raw_title[:25])

            clean_title = re.sub(r"\s+", " ", raw_title).strip()
            full_link = f"{base_url}{href}" if href.startswith("?") else href

            # Capture ANY tender unconditionally
            results.append({
                "source": portal_label.split(" (")[0],
                "tender_id": tender_id or clean_title[:20],
                "department": department,
                "title": clean_title,
                "closing_date": closing_date,
                "link": full_link or base_url
            })

        print(f"[{portal_label}] Captured {len(results)} tenders.")

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
            tenders = await scrape_all_tenders(label, url, ocr, context)
            all_tenders.extend(tenders)

        await browser.close()

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())
