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
        raise ValueError("Error: GCP_CREDENTIALS environment secret not found.")
    
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(credentials)
    return gc.open(SHEET_NAME).sheet1

def save_to_sheet(sheet, tenders):
    if not tenders:
        print("\n--> No matching tenders found across all queried portals today.")
        return

    existing_records = sheet.get_all_values()
    existing_ids = set()
    if len(existing_records) > 1:
        # Check Column C (Tender ID) to skip duplicates
        existing_ids = {row[2].strip() for row in existing_records[1:] if len(row) > 2}

    new_rows = []
    today = datetime.now().strftime("%Y-%m-%d")

    for item in tenders:
        if item["tender_id"] not in existing_ids:
            new_rows.append([
                today,
                item["source"],
                item["tender_id"],
                item["title"],
                item["closing_date"],
                item["link"]
            ])

    if new_rows:
        sheet.append_rows(new_rows)
        print(f"\nSUCCESS: Added {len(new_rows)} new tenders to '{SHEET_NAME}'!")
    else:
        print("\nAll identified tenders are already tracked in the Google Sheet.")

# Expanded portal list covering State, Central, and Major Infrastructure boards
PORTALS = {
    "Maharashtra (MahaTenders / CIDCO / MMRDA / PMRDA)": "https://mahatenders.gov.in/nicgep/app",
    "Goa eProcure (GSIDC / PWD)": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh (MP Tenders / MPRDC / Metro)": "https://mptenders.gov.in/nicgep/app",
    "CPPP Central Portal (NHAI / MoRTH / Rail / Urban)": "https://eprocure.gov.in/eprocure/app"
}

# Targeted keywords for consultancy, transport, and project management
KEYWORDS = [
    r"\bpmc\b",
    r"project management consult",
    r"feasibility",
    r"\bdpr\b",
    r"detailed project report",
    r"urban mobility",
    r"transit",
    r"metro",
    r"brts",
    r"bus terminal",
    r"multimodal",
    r"multi-modal",
    r"authority engineer",
    r"independent engineer",
    r"highways?",
    r"expressway",
    r"consultan",
    r"traffic survey",
    r"comprehensive mobility plan",
    r"\bcmp\b",
    r"flyover",
    r"ring road",
    r"smart city",
    r"town planning",
    r"infrastructure advisory"
]
KEYWORD_REGEX = re.compile("|".join(KEYWORDS), re.IGNORECASE)

async def solve_captcha(page, selector, ocr):
    captcha_el = await page.wait_for_selector(selector, timeout=10000)
    image_bytes = await captcha_el.screenshot()
    solved = ocr.classification(image_bytes)
    return solved.strip().replace(" ", "")

async def scrape_portal(portal_label, base_url, ocr):
    print(f"\n--------------------------------------------------")
    print(f"Querying: {portal_label}")
    print(f"--------------------------------------------------")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # Direct Active Tenders endpoint used by GePNIC engines
            search_url = f"{base_url}?page=FrontEndLatestActiveTenders&service=page"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2)

            # Solve CAPTCHA challenge if triggered
            captcha_img = await page.query_selector("#captchaImage")
            if captcha_img:
                for attempt in range(4):
                    code = await solve_captcha(page, "#captchaImage", ocr)
                    print(f"[{portal_label}] Solved captcha: '{code}' (Attempt {attempt + 1})")
                    
                    await page.fill("#captchaText", code)
                    submit_btn = await page.query_selector("#Submit")
                    if submit_btn:
                        await submit_btn.click()
                    
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(2)

                    err = await page.query_selector("text='Invalid Captcha'")
                    if not err and not await page.query_selector("#captchaText"):
                        print(f"[{portal_label}] Captcha verified successfully.")
                        break
                    
                    refresh_btn = await page.query_selector("#Image1")
                    if refresh_btn:
                        await refresh_btn.click()
                        await asyncio.sleep(1)

            # Select results table
            rows = await page.query_selector_all("#table tr, table.list_table tr")
            print(f"[{portal_label}] Total tender entries on page: {len(rows)}")

            matches = []
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                row_text = (await row.inner_text()).strip()

                if KEYWORD_REGEX.search(row_text):
                    link_el = await row.query_selector("a")
                    title = ""
                    href = ""
                    if link_el:
                        title = (await link_el.inner_text()).strip()
                        href = await link_el.get_attribute("href") or ""
                    else:
                        title = (await cells[len(cells)-2].inner_text()).strip()

                    closing_date = (await cells[2].inner_text()).strip() if len(cells) > 2 else "N/A"
                    tender_id = (await cells[len(cells)-1].inner_text()).strip() if len(cells) > 3 else "N/A"

                    clean_title = title.replace("\n", " ").strip()
                    full_link = f"{base_url}{href}" if href.startswith("?") else href

                    matches.append({
                        "source": portal_label.split(" (")[0],
                        "tender_id": tender_id or clean_title[:20],
                        "title": clean_title,
                        "closing_date": closing_date,
                        "link": full_link
                    })

            print(f"[{portal_label}] Matching tenders: {len(matches)}")
            return matches

        except Exception as e:
            print(f"[{portal_label}] Crawl issue encountered: {e}")
            return []
        finally:
            await browser.close()

async def main():
    sheet = get_sheet()
    ocr = ddddocr.DdddOcr(show_ad=False)
    
    all_tenders = []
    for label, url in PORTALS.items():
        tenders = await scrape_portal(label, url, ocr)
        all_tenders.extend(tenders)

    save_to_sheet(sheet, all_tenders)

if __name__ == "__main__":
    asyncio.run(main())
