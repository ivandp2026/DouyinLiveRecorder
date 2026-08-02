from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"Cannot locate {label}")
    return text.replace(old, new, 1)


main_path = Path("main.py")
main = main_path.read_text(encoding="utf-8-sig")
if "from src.douyu_streamlink import (" not in main:
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

# Reconnect options are input options. They must be placed before -i and
# boolean options need explicit values. This prevents short CDN/network
# interruptions from ending a Douyu recording after only a few minutes.
old_reconnect = '''                                    "-fflags", "+discardcorrupt",
                                    "-re", "-i", real_url,
                                    "-bufsize", bufsize,
                                    "-sn", "-dn",
                                    "-reconnect_delay_max", "60",
                                    "-reconnect_streamed", "-reconnect_at_eof",
                                    "-max_muxing_queue_size", max_muxing_queue_size,
'''
new_reconnect = '''                                    "-fflags", "+discardcorrupt",
                                    "-reconnect", "1",
                                    "-reconnect_streamed", "1",
                                    "-reconnect_at_eof", "1",
                                    "-reconnect_on_network_error", "1",
                                    "-reconnect_on_http_error", "4xx,5xx",
                                    "-reconnect_delay_max", "60",
                                    "-i", real_url,
                                    "-bufsize", bufsize,
                                    "-sn", "-dn",
                                    "-max_muxing_queue_size", max_muxing_queue_size,
'''
main = replace_once(main, old_reconnect, new_reconnect, "FFmpeg reconnect options")

# Split recording conversion scans the whole directory. Sidecar files created
# by danmaku analysis share the same filename prefix and were therefore sent
# to FFmpeg as if they were videos. Restrict conversion to real media files.
old_conversion = '''                for path in file_paths:
                    if prefix in path:
                        threading.Thread(target=converts_mp4, args=(path, delete_origin_file)).start()
'''
new_conversion = '''                video_extensions = {'.ts', '.flv', '.mkv', '.mov', '.m4v', '.webm'}
                for path in file_paths:
                    suffix = Path(path).suffix.lower()
                    if prefix in os.path.basename(path) and suffix in video_extensions:
                        threading.Thread(target=converts_mp4, args=(path, delete_origin_file)).start()
'''
if "suffix in VIDEO_EXTENSIONS" not in main:
    main = replace_once(main, old_conversion, new_conversion, "sidecar conversion filter")

main_path.write_text(main, encoding="utf-8-sig")
print("Douyu recording uses Streamlink with stable reconnect and safe conversion filtering")
