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
    """Appends genuine tenders to Google Sheet without duplicating existing IDs."""
    if not tenders:
        print("\n--> No valid tenders found to append.")
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
        if t_id and t_id not in existing_ids:
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
        print("\nAll matching tenders are already present in the sheet.")

PORTALS = {
    "Maharashtra": "https://mahatenders.gov.in/nicgep/app",
    "Goa": "https://eprocure.goa.gov.in/nicgep/app",
    "Madhya Pradesh": "https://mptenders.gov.in/nicgep/app",
    "Central CPPP": "https://eprocure.gov.in/eprocure/app"
}

# Regex to detect Indian tender dates like "24-Oct-2026", "15-Nov-2026 03:00 PM"
DATE_PATTERN = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b")

# Comprehensive keyword filter for transport, PMC, advisory, and urban mobility
FILTER_REGEX = re.compile(
    r"("
    # EV & Modern Bus Transit
    r"electric\s+bus|e-?bus|ebuses|charging\s+infra|evse|fast\s+charger|battery\s+swapp|"
    r"gross\s+cost\s+contract|\bgcc\b|depot\s+charging|fleet\s+electrif|"
    # Urban Transit Assets & Amenities
    r"bus\s+shelter|bus\s+queue\s+shelter|\bbqs\b|ac\s+bus\s+shelter|bus\s+terminal|"
    r"foot\s*over\s*bridge|\bfob\b|skywalk|pedestrian\s+underpass|subway|"
    r"multi.?modal|mmlp|intermodal|transit.oriented|\btod\b|"
    # Parking & Traffic Engineering
    r"multi.?level\s+car\s+parking|\bmlcp\b|parking\s+management|on-?street\s+parking|"
    r"fastag\s+parking|congestion|traffic\s+engineering|traffic\s+survey|traffic\s+study|"
    r"junction\s+improvement|signaliz|adaptive\s+traffic|\batcs\b|\bits\b|blackspot|"
    # Urban Mobility & Active Transport
    r"urban\s+mobility|comprehensive\s+mobility|\bcmp\b|non.?motorized|\bnmt\b|"
    r"cycle\s+track|complete\s+streets|street\s+design|pedestrianiz|"
    # Highways, Metro & Corridors
    r"metro\s+rail|metro\s+neo|metro\s+lite|brts|ropeway|cable\s+car|"
    r"highways?|expressway|ring\s+road|flyover|elevated\s+corridor|bypass|tunnels?|"
    # Institutional Strengthening & Program Management
    r"technical\s+assistance|capacity\s+strengthen|capacity\s+build|institutional\s+strengthen|"
    r"project\s+management\s+unit|\bpmu\b|project\s+implementation\s+unit|\bpiu\b|"
    r"\bpmc\b|project\s+management\s+consult|project\s+monitoring|independent\s*engineer|"
    r"authority['\s]*s?\s*engineer|proof\s+consultant|technical\s+audit|"
    # Financial, Transaction Advisory & Delivery Models
    r"transaction\s+advis|financial\s+advis|revenue\s+model|tariff\s+study|"
    r"\bppp\b|dbfot|\bham\b|hybrid\s+annuity|viability\s+gap|\bvgf\b|concession\s+agreement|"
    # Procurement Types & Project Reports
    r"empanelment|expression\s+of\s+interest|\beoi\b|\brfp\b|\brfq\b|pre-qualification|"
    r"detailed\s+project\s+report|\bdpr\b|feasibility\s+study|detailed\s+design|smart\s+city"
    r")",
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

        # 1. Handle CAPTCHA Verification
        captcha_img = await page.query_selector("#captchaImage")
        if captcha_img:
            for attempt in range(5):
                code = await solve_captcha(page, "#captchaImage", ocr)
                print(f"[{portal_label}] Attempt {attempt + 1}: Solved as '{code}'")

                captcha_input = await page.wait_for_selector("#captchaText")
                await captcha_input.fill("")
                await captcha_input.type(code, delay=50)
                await captcha_input.press("Enter")

                submit_btn = await page.query_selector("#Submit")
                if submit_btn:
                    try:
                        await submit_btn.click(timeout=3000)
                    except Exception:
                        pass

                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)

                still_has_captcha = await page.query_selector("#captchaText")
                if not still_has_captcha:
                    print(f"[{portal_label}] CAPTCHA bypassed successfully!")
                    break

                refresh_btn = await page.query_selector("#Image1")
                if refresh_btn:
                    await refresh_btn.click()
                    await asyncio.sleep(2)

        # 2. TARGET ONLY THE DATA GRID ROWS (tr.even and tr.odd inside #table)
        # This completely ignores navigation menus, tickers, headers, and footers!
        data_rows = await page.query_selector_all("#table tr.even, #table tr.odd, table.list_table tr.even, table.list_table tr.odd")
        print(f"[{portal_label}] Real tender rows in data grid: {len(data_rows)}")

        for row in data_rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 4:
                continue

            row_text = (await row.inner_text()).strip()

            # A legitimate tender MUST contain a valid date in its cells
            date_match = DATE_PATTERN.search(row_text)
            if not date_match:
                continue

            # Check keyword filter (matches our transport/PMC/advisory domain)
            if not FILTER_REGEX.search(row_text):
                continue

            # Extract tender link and title from the row
            link_el = await row.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else ""
            title = (await link_el.inner_text()).strip() if link_el else row_text[:120]

            # In GePNIC data rows:
            # Cell 0: S.No (e.g. 1)
            # Cell 1: e-Published Date
            # Cell 2: Closing Date
            # Cell 3: Opening Date
            # Cell 4: Title and Ref No / Tender ID
            closing_date = "Check Link"
            if len(cells) >= 3:
                c_date = (await cells[2].inner_text()).strip()
                if DATE_PATTERN.search(c_date):
                    closing_date = c_date

            # Extract Tender ID from row text
            id_match = re.search(r"(\d{4}_[A-Z0-9]+_\d+|\b[0-9]{6,}\b)", row_text)
            tender_id = id_match.group(0) if id_match else re.sub(r"\W+", "_", title[:25])

            department = "State Authority / ULB"
            full_link = f"{base_url}{href}" if href.startswith("?") else href

            results.append({
                "source": portal_label,
                "tender_id": tender_id,
                "department": department,
                "title": re.sub(r"\s+", " ", title),
                "closing_date": closing_date,
                "link": full_link or base_url
            })

        print(f"[{portal_label}] Matched relevant opportunities: {len(results)}")

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
