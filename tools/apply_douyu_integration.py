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

print("Douyu integration applied")
