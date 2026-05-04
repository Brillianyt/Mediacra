# -*- coding: utf-8 -*-
import asyncio
import json
import os
from dotenv import load_dotenv
import httpx

load_dotenv()

async def main():
    base = (os.getenv('MEMFIRE_BASE_URL') or '').strip().strip('"').strip("'").rstrip('/')
    token = (os.getenv('MEMFIRE_SERVICE_ROLE_KEY') or '').strip().strip('"').strip("'")
    headers = {
        'apikey': token,
        'Authorization': f'Bearer {token}',
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f'{base}/rest/v1/activities?select=*&limit=1', headers=headers)
        print('status=', resp.status_code)
        data = resp.json()
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        if data:
            print('keys=', sorted(list(data[0].keys())))

asyncio.run(main())
