from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"Cannot locate {label}")
    return text.replace(old, new, 1)


main_path = Path("main.py")
main = main_path.read_text(encoding="utf-8-sig")
main = replace_once(
    main,
    "from src.douyu_danmaku import DouyuDanmakuSession\n",
    "from src.douyu_danmaku import DouyuDanmakuSession\nfrom src.douyu_streamlink import resolve_douyu_stream\n",
    "Douyu Streamlink import",
)
old = '''                    elif record_url.find("https://www.douyu.com/") > -1:
                        platform = '斗鱼直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_douyu_info_data(
                                url=record_url, proxy_addr=proxy_address, cookies=douyu_cookie))
                            port_info = asyncio.run(stream.get_douyu_stream_url(
                                json_data, video_quality=record_quality, cookies=douyu_cookie, proxy_addr=proxy_address
                            ))
'''
new = '''                    elif record_url.find("https://www.douyu.com/") > -1:
                        platform = '斗鱼直播'
                        with semaphore:
                            port_info = resolve_douyu_stream(
                                record_url,
                                quality=record_quality,
                                cookies=douyu_cookie,
                                proxy=proxy_address or "",
                            )
'''
main = replace_once(main, old, new, "Douyu main routing")
main_path.write_text(main, encoding="utf-8-sig")
print("Douyu recording now uses Streamlink")
