# -*- coding: utf-8 -*-
import asyncio
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
    sql = """
    select column_name, data_type
    from information_schema.columns
    where table_schema = 'public' and table_name = 'activities'
    order by ordinal_position;
    """.strip()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f'{base}/rest/v1/rpc/exec_sql', headers={**headers, 'Content-Type': 'application/json'}, json={'sql': sql})
        print('rpc_status=', resp.status_code)
        print(resp.text[:2000])
        if resp.status_code < 400:
            return
        alt = await client.get(f"{base}/rest/v1/information_schema.columns?table_schema=eq.public&table_name=eq.activities&select=column_name,data_type&order=ordinal_position", headers=headers)
        print('table_status=', alt.status_code)
        print(alt.text[:2000])

asyncio.run(main())
