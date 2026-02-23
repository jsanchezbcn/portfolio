import asyncio
from playwright.async_api import async_playwright

async def test_dashboard():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print("Navigating to dashboard...")
        await page.goto("http://localhost:8506")
        
        print("Waiting for 5 seconds...")
        await asyncio.sleep(5)
        
        print("Taking screenshot...")
        await page.screenshot(path="dashboard_smoke_test.png")
        print("Screenshot saved to dashboard_smoke_test.png")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_dashboard())
