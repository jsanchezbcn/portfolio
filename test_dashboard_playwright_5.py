import asyncio
from playwright.async_api import async_playwright

async def test_dashboard():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print("Navigating to dashboard...")
        await page.goto("http://localhost:8506", wait_until="domcontentloaded")
        
        print("Waiting for dashboard to load...")
        await page.wait_for_selector(".stApp", timeout=30000)
        
        await asyncio.sleep(5)
        
        content = await page.content()
        with open("/tmp/dashboard_html.txt", "w") as f:
            f.write(content)
            
        print("HTML saved to /tmp/dashboard_html.txt")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_dashboard())
