import asyncio
import os

import httpx
from dotenv import load_dotenv


load_dotenv()


async def main() -> None:
    base = (os.getenv("MEMFIRE_BASE_URL") or "").strip().strip('"').strip("'").rstrip("/")
    token = (os.getenv("MEMFIRE_SERVICE_ROLE_KEY") or "").strip().strip('"').strip("'")
    headers = {
        "apikey": token,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,missing=default",
    }
    payload = {
        "title": "tmp probe activity",
        "start_time": "2026-04-20T14:00:00+08:00",
        "end_time": "2026-04-20T17:00:00+08:00",
        "location_name": "Shanghai",
        "brief": "tmp probe",
        "original_link": "https://example.com/probe",
        "highlights": ["probe"],
        "category": "活动",
        "cover_image_url": None,
        "organizer": "probe",
        "is_featured": False,
        "is_active": True,
        "is_trending": False,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{base}/rest/v1/activities", headers=headers, json=payload)
        print("status=", response.status_code)
        print(response.text[:4000])


if __name__ == "__main__":
    asyncio.run(main())
