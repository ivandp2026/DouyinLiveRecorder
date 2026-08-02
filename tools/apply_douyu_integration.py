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
    "from src.danmaku import DanmakuSession\n",
    "from src.danmaku import DanmakuSession\nfrom src.douyu_danmaku import DouyuDanmakuSession\n",
    "danmaku import",
)
main = replace_once(
    main,
    "    enable_douyin_danmaku = options.get(\n        read_config_value(config, '弹幕分析', '是否开启抖音弹幕分析(是/否)', \"否\"), False\n    )\n",
    "    enable_douyin_danmaku = options.get(\n        read_config_value(config, '弹幕分析', '是否开启抖音弹幕分析(是/否)', \"否\"), False\n    )\n    enable_douyu_danmaku = options.get(\n        read_config_value(config, '弹幕分析', '是否开启斗鱼弹幕分析(是/否)', \"是\"), True\n    )\n",
    "Douyu config",
)
old = '''    danmaku_session = None
    if enable_douyin_danmaku and "douyin.com/" in record_url:
        try:
            danmaku_session = DanmakuSession(
                save_file_path, douyin_room_ids.get(record_url, record_url), dy_cookie, danmaku_collector_command,
                highlight_options=danmaku_highlight_options,
                relay_executable=danmaku_relay_executable,
                relay_port=danmaku_relay_port,
                duplicate_window=danmaku_duplicate_window,
            )
            danmaku_session.start()
            logger.info(f"[{record_name}] 抖音弹幕分析已启动")
        except Exception as e:
            danmaku_session = None
            logger.error(f"[{record_name}] 抖音弹幕分析启动失败: {e}")
'''
new = '''    danmaku_session = None
    is_douyin = "douyin.com/" in record_url
    is_douyu = "douyu.com/" in record_url
    if (enable_douyin_danmaku and is_douyin) or (enable_douyu_danmaku and is_douyu):
        try:
            session_class = DouyuDanmakuSession if is_douyu else DanmakuSession
            session_url = record_url if is_douyu else douyin_room_ids.get(record_url, record_url)
            session_cookie = douyu_cookie if is_douyu else dy_cookie
            session_command = "" if is_douyu else danmaku_collector_command
            danmaku_session = session_class(
                save_file_path, session_url, session_cookie, session_command,
                highlight_options=danmaku_highlight_options,
                relay_executable=danmaku_relay_executable,
                relay_port=danmaku_relay_port,
                duplicate_window=danmaku_duplicate_window,
            )
            danmaku_session.start()
            platform_name = "斗鱼" if is_douyu else "抖音"
            logger.info(f"[{record_name}] {platform_name}弹幕分析已启动")
        except Exception as e:
            danmaku_session = None
            platform_name = "斗鱼" if is_douyu else "抖音"
            logger.error(f"[{record_name}] {platform_name}弹幕分析启动失败: {e}")
'''
main = replace_once(main, old, new, "check_subprocess integration")
main_path.write_text(main, encoding="utf-8-sig")

config_path = Path("config/config.ini")
config = config_path.read_text(encoding="utf-8-sig")
if "是否开启斗鱼弹幕分析(是/否)" not in config:
    marker = "是否开启抖音弹幕分析(是/否) = 是\n"
    if marker not in config:
        marker = "是否开启抖音弹幕分析(是/否) = 否\n"
    if marker not in config:
        raise RuntimeError("Cannot locate danmaku config section")
    config = config.replace(marker, marker + "是否开启斗鱼弹幕分析(是/否) = 是\n", 1)
    config_path.write_text(config, encoding="utf-8-sig")

# Rebuild the brittle Douyu path. Numeric room URLs no longer require parsing
# the mobile page, and stream discovery first uses the H5 preview endpoint.
spider_path = Path("src/spider.py")
spider = spider_path.read_text(encoding="utf-8-sig")
spider = replace_once(
    spider,
    '''    match_rid = re.search('rid=(.*?)(?=&|$)', url)
    if match_rid:
        rid = match_rid.group(1)
    else:
        rid = re.search('douyu.com/(.*?)(?=\\\\?|$)', url).group(1)
        html_str = await async_req(url=f'https://m.douyu.com/{rid}', proxy_addr=proxy_addr, headers=headers)
        json_str = re.findall('<script id="vike_pageContext" type="application/json">(.*?)</script>', html_str)[0]
        json_data = json.loads(json_str)
        rid = json_data['pageProps']['room']['roomInfo']['roomInfo']['rid']
''',
    '''    parsed_url = urllib.parse.urlparse(url)
    query_rid = urllib.parse.parse_qs(parsed_url.query).get('rid', [''])[0]
    path_rid = parsed_url.path.strip('/').split('/')[0] if parsed_url.path.strip('/') else ''
    rid = query_rid or path_rid
    if not rid:
        raise ValueError(f"无法从斗鱼链接提取房间号: {url}")
    if not rid.isdigit():
        html_str = await async_req(url=f'https://m.douyu.com/{rid}', proxy_addr=proxy_addr, headers=headers)
        room_patterns = (
            r'"rid"\\s*:\\s*"?(\\d+)"?',
            r'"room_id"\\s*:\\s*"?(\\d+)"?',
            r'roomId\\s*[:=]\\s*["\\\']?(\\d+)',
        )
        real_rid = None
        for pattern in room_patterns:
            room_match = re.search(pattern, html_str)
            if room_match:
                real_rid = room_match.group(1)
                break
        if not real_rid:
            raise ValueError(f"无法解析斗鱼真实房间号: {url}")
        rid = real_rid
''',
    "Douyu room id parsing",
)
spider = replace_once(
    spider,
    '''    did = '10000000000000000000000000003306'
    params_list = await get_token_js(rid, did, proxy_addr=proxy_addr)
    headers = {
''',
    '''    did = '10000000000000000000000000003306'
    preview_headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/121 Mobile Safari/537.36',
        'Referer': f'https://m.douyu.com/{rid}',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    if cookies:
        preview_headers['Cookie'] = cookies
    try:
        preview_api = f'https://playweb.douyucdn.cn/lapi/live/hlsH5Preview/{rid}'
        preview_body = urllib.parse.urlencode({'rid': rid, 'did': did}).encode('utf-8')
        preview_str = await async_req(
            url=preview_api, proxy_addr=proxy_addr, headers=preview_headers, data=preview_body
        )
        preview_json = json.loads(preview_str)
        preview_data = preview_json.get('data') or {}
        rtmp_url = preview_data.get('rtmp_url') or preview_data.get('rtmpUrl')
        rtmp_live = preview_data.get('rtmp_live') or preview_data.get('rtmpLive')
        if rtmp_url and rtmp_live:
            return {'data': {'rtmp_url': rtmp_url, 'rtmp_live': rtmp_live}}
        hls_url = preview_data.get('hls_url') or preview_data.get('hlsUrl')
        if hls_url:
            base_url, stream_name = hls_url.rsplit('/', maxsplit=1)
            return {'data': {'rtmp_url': base_url, 'rtmp_live': stream_name}}
    except Exception as preview_error:
        print(f"斗鱼H5预览接口不可用，切换签名接口: {preview_error}")

    params_list = await get_token_js(rid, did, proxy_addr=proxy_addr)
    if len(params_list) < 4:
        raise RuntimeError('斗鱼签名参数解析失败，请更新程序或配置斗鱼Cookie')
    headers = {
''',
    "Douyu H5 stream fallback",
)
spider_path.write_text(spider, encoding="utf-8-sig")

print("Douyu integration and video parser fixes applied")
