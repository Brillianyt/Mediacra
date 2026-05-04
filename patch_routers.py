with open('api/routers/__init__.py', 'r', encoding='utf-8') as f:
    content = f.read()

if 'ai_router' not in content:
    content = content.replace('from .scheduler import router as scheduler_router', 'from .scheduler import router as scheduler_router\nfrom .ai import router as ai_router')
    content = content.replace('"scheduler_router",', '"scheduler_router",\n    "ai_router",')
    with open('api/routers/__init__.py', 'w', encoding='utf-8') as f:
        f.write(content)

with open('api/main.py', 'r', encoding='utf-8') as f:
    content = f.read()

if 'ai_router' not in content:
    content = content.replace('feishu_router, scheduler_router,', 'feishu_router, scheduler_router, ai_router,')
    content = content.replace('app.include_router(scheduler_router, prefix="/api")', 'app.include_router(scheduler_router, prefix="/api")\napp.include_router(ai_router, prefix="/api")')
    with open('api/main.py', 'w', encoding='utf-8') as f:
        f.write(content)
