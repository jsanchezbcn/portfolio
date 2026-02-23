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
        
        print("Waiting for Margin Usage...")
        try:
            await page.wait_for_selector("text=Margin Usage", timeout=15000)
            print("✅ Margin Usage metric found")
        except Exception as e:
            print(f"❌ Margin Usage metric NOT found: {e}")
            
        try:
            await page.wait_for_selector("text=SPX", timeout=5000)
            print("✅ SPX Weighted Delta metric found")
        except Exception as e:
            print(f"❌ SPX Weighted Delta metric NOT found: {e}")
            
        try:
            await page.wait_for_selector("text=Vega Exposure", timeout=5000)
            print("✅ Vega Exposure metric found")
        except Exception as e:
            print(f"❌ Vega Exposure metric NOT found: {e}")
            
        try:
            await page.wait_for_selector("text=Theta/Vega Ratio", timeout=5000)
            print("✅ Theta/Vega Ratio metric found")
        except Exception as e:
            print(f"❌ Theta/Vega Ratio metric NOT found: {e}")
            
        await page.screenshot(path="dashboard_smoke_test_final.png")
        print("Screenshot saved to dashboard_smoke_test_final.png")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_dashboard())
