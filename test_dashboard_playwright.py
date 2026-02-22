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
        
        print("Checking for key elements...")
        
        content = await page.content()
        
        if "Margin Usage" in content:
            print("✅ Margin Usage metric found")
        else:
            print("❌ Margin Usage metric NOT found")
            
        if "SPX" in content and "Weighted Delta" in content:
            print("✅ SPX Weighted Delta metric found")
        else:
                                                                                  if "Vega Exposure" in content:
            print("✅ Vega Exposure             print("�   else:            print("✅ Vega Exposure  me            print("✅ Vega Exposure             prinRatio" in content:
            print("✅ Theta/Vega Ratio metric found")
        else:
            print("❌ Theta/Vega Ratio metric NOT found")
            
        await page.screenshot(path="dashboard_smoke_test.png")
        print("Screenshot saved to dashboard_smoke_test.png")
        
        await browser.close()

if __name__ =if"__main__":
    asyncio.run(test_dashboard())
