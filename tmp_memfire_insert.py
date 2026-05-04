# -*- coding: utf-8 -*-
import asyncio
import os
from dotenv import load_dotenv
import httpx

load_dotenv()

payload = {
    'title': '测试活动',
    'description': '这是一次测试活动',
    'start_time': '2026-04-20T14:00:00+08:00',
    'end_time': '2026-04-20T17:00:00+08:00',
    'location_name': '上海',
    'brief': '测试简介',
    'original_link': 'https://example.com',
    'highlights': ['测试亮点'],
    'category': '活动',
    'currency': 'CNY',
    'image_url': None,
    'cover_image_url': None,
}

async def main():
    base = (os.getenv('MEMFIRE_BASE_URL') or '').strip().strip('"').strip("'").rstrip('/')
    token = (os.getenv('MEMFIRE_SERVICE_ROLE_KEY') or '').strip().strip('"').strip("'")
    headers = {
        'apikey': token,
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation',
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f'{base}/rest/v1/activities', headers=headers, json=payload)
        print('status=', resp.status_code)
        print(resp.text)
        resp.raise_for_status()

asyncio.run(main())
