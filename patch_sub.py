import re

with open('api/routers/subscription.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the save_option logic to fallback correctly
def replace_save_option(match):
    return """
    import config
    configured_save_option = str(getattr(config, "SAVE_DATA_OPTION", "json") or "json").lower()
    save_option = configured_save_option
    if save_option == "mysql":
        save_option = "db"
    if save_option not in {"csv", "db", "json", "sqlite", "mongodb", "excel", "postgres"}:
        save_option = "json"

    start_request = CrawlerStartRequest(
        platform=sub.platform,
        login_type=crawl_config.get("login_type", "cookie"),
        crawler_type="creator",
        creator_ids=sub.creator_id,
        save_option=save_option,
        enable_comments=crawl_config.get("enable_comments", False),
        enable_sub_comments=crawl_config.get("enable_sub_comments", False),
        headless=crawl_config.get("headless", True),
    )"""

pattern = re.compile(r'start_request = CrawlerStartRequest\([^)]+\)', re.DOTALL)
if pattern.search(content):
    content = pattern.sub(replace_save_option, content)
    with open('api/routers/subscription.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patch applied")
else:
    print("Pattern not found")
