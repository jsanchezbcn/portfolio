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
        
        success = True
        
        if await page.locator("text=Margin Usage").is_visible():
            print("✅ Margin Usage metric found")
        else:
            print("❌ Margin Usage metric NOT found")
            success = False
            
        if await page.locator("text=SPX β-Weighted Delta").is_visible() or await page.locator("text=SPX").is_visible():
            print("✅ SPX Weighted Delta metric found")
        else:
            print("❌ SPX Weighted Delta metric NOT found")
            success = False
            
        if await page.locator("text=Vega Exposure").is_visible():
            print("✅ Vega Exposure metric found")
        else:
            print("❌ Vega Exposure metric NOT found")
            success = False
            
        if await page.locator("text=Theta/Vega Ratio").is_visible():
            print("✅ Theta/Vega Ratio metric found")
        else:
            print("❌ Theta/Vega Ratio metric NOT found")
            success = False
            
        await page.screenshot(path="dashboard_smoke_test_final.png")
        print("Screenshot saved to dashboard_smoke_test_final.png")
        
        await browser.close()
        
        if success:
            print("ALL TESTS PASSED!")
        else:
            print("SOME TESTS FAILED!")

if __name__ == "__main__":
    asyncio.run(test_dashboard())
