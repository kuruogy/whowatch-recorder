#!/usr/bin/env python3
"""
ふわっち自動録画アプリ - Windows GUI版
Flask + ブラウザ UI で動作。start.bat をダブルクリックして使う。
"""

import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests
import websockets
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

# ─── パス設定 ────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
OUTPUT_DIR  = Path.home() / "Videos" / "whowatch"
STATIC_DIR  = BASE_DIR / "static"

DEFAULT_CONFIG = {
    "streamers": [],
    "check_interval": 30,
    "quality": "2high",
    "output_dir": str(OUTPUT_DIR),
    "save_comments": True,
    "embed_subtitles": True,
}

API_BASE  = "https://api.whowatch.tv"
# Phoenix Channels WebSocket
WS_BASE = "wss://ws.whowatch.tv/socket/websocket?vsn=2.0.0"
WS_CANDIDATES = [WS_BASE]
WS_COMMENT = WS_BASE

# ─── Flask / SocketIO ────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["SECRET_KEY"] = "whowatch-recorder-secret"
sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ─── 状態 ────────────────────────────────────────────────
recording_sessions: dict[str, dict] = {}   # user_path -> {live_id, proc, start_time, video_file}
log_buffer: list[dict] = []                # ログ履歴（最大 500 件）
_lock = threading.Lock()

# ─── ユーティリティ ──────────────────────────────────────

def now_str(fmt="%Y%m%d_%H%M%S"):
    return datetime.datetime.now().strftime(fmt)

def push_log(msg: str, level: str = "info"):
    entry = {"time": datetime.datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    log_buffer.append(entry)
    if len(log_buffer) > 500:
        log_buffer.pop(0)
    sio.emit("log", entry)
    print(f"[{entry['time']}][{level.upper()}] {msg}", flush=True)

def push_status():
    sio.emit("status", build_status())

def build_status():
    cfg = load_config()
    sessions = []
    with _lock:
        for up, s in recording_sessions.items():
            sessions.append({
                "user_path": up,
                "live_id":   s["live_id"],
                "start_time": s["start_time"],
                "video_file": s["video_file"],
            })
    return {
        "streamers": cfg["streamers"],
        "recording": sessions,
        "config": cfg,
    }

# ─── 設定 ────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ─── ふわっち API ────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer":    "https://whowatch.tv/",
    "Origin":     "https://whowatch.tv",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

def normalize_user_path(user_path: str) -> str:
    """入力を w:xxxx 形式に正規化する"""
    up = user_path.strip()
    if ":" in up:
        prefix, name = up.split(":", 1)
        # ふ: → w: に変換
        if prefix in ("ふ",):
            return f"w:{name}"
        return up
    # prefix なし → w: を付ける
    return f"w:{up}"

# /lives レスポンスをキャッシュ（同一チェック周期内で使い回す）
_lives_cache: list = []
_lives_cache_time: float = 0.0
_LIVES_CACHE_TTL = 20  # 秒

def _fetch_lives() -> list:
    """配信中一覧を取得してキャッシュする"""
    global _lives_cache, _lives_cache_time
    import time as _time
    now = _time.time()
    if now - _lives_cache_time < _LIVES_CACHE_TTL and _lives_cache:
        return _lives_cache
    try:
        r = requests.get(f"{API_BASE}/lives", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            _lives_cache = r.json()
            _lives_cache_time = now
            return _lives_cache
        else:
            push_log(f"/lives [{r.status_code}]", "warn")
    except Exception as e:
        push_log(f"/lives 取得エラー: {e}", "warn")
    return _lives_cache  # 失敗時は古いキャッシュを返す

def get_live_id(user_path: str) -> str | None:
    """
    live_id を取得する。2段階で検索:
    1. /lives 全件リストから検索
    2. /lives?user_path={user_path} で直接検索
    3. /users/{user_path}/current_live で直接取得
    """
    target = normalize_user_path(user_path)

    # 方法1: /lives 一覧から検索
    categories = _fetch_lives()
    for category in categories:
        for order in ("new", "popular"):
            for live in category.get(order, []):
                up = live.get("user", {}).get("user_path", "")
                if up == target:
                    live_id = str(live.get("id", ""))
                    push_log(f"配信検知: {target} live_id={live_id}", "start")
                    return live_id

    # 方法2: user_path で直接検索
    try:
        # user_path形式: "w:username" → URLエンコード不要
        r = requests.get(f"{API_BASE}/lives?user_path={target}",
                         headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            lives = data if isinstance(data, list) else data.get("lives", [])
            if lives:
                live_id = str(lives[0].get("id", ""))
                if live_id:
                    push_log(f"配信検知(直接): {target} live_id={live_id}", "start")
                    return live_id
    except Exception:
        pass

    # 方法3: /users/{user_path}/current_live
    try:
        encoded = target.replace(":", "%3A")
        r = requests.get(f"{API_BASE}/users/{encoded}/current_live",
                         headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            live = data.get("live") or data
            live_id = str(live.get("id", ""))
            if live_id and live_id != "0":
                push_log(f"配信検知(current): {target} live_id={live_id}", "start")
                return live_id
    except Exception:
        pass

    # 方法4: /lives/{user_path} 形式を試す
    try:
        encoded = target.replace(":", "%3A")
        for path in [f"/lives/user/{encoded}", f"/user/{encoded}/live"]:
            r = requests.get(f"{API_BASE}{path}", headers=HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json()
                live_id = str((data.get("live") or data).get("id", ""))
                if live_id and live_id != "0":
                    push_log(f"配信検知(path): {target} live_id={live_id}", "start")
                    return live_id
    except Exception:
        pass

    return None

def get_live_data(live_id: str) -> dict:
    """
    live_id の詳細データを取得する。
    - /lives/{id}?last_updated_at={ts} → jwt, ws_url
    - /lives/{id}/play                 → hls_url
    戻り値: {"hls_url": ..., "jwt": ..., "ws_url": ...}
    """
    import time as _time
    ts = int(_time.time() * 1000)
    result = {}

    # 1. jwt と ws_url を取得
    try:
        url = f"{API_BASE}/lives/{live_id}?last_updated_at={ts}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            result["jwt"] = data.get("jwt", "")
            result["ws_url"] = data.get("comment_server_url", "wss://ws.whowatch.tv/socket")
            if result["jwt"]:
                push_log("JWT取得成功", "info")
    except Exception as e:
        push_log(f"jwt取得エラー: {e}", "warn")

    # 2. hls_url を /play エンドポイントから取得
    try:
        url2 = f"{API_BASE}/lives/{live_id}/play"
        r2 = requests.get(url2, headers=HEADERS, timeout=10)
        if r2.status_code == 200:
            data2 = r2.json()
            streams = data2.get("streams") or []
            hls_url = None
            for s in streams:
                if not s.get("audio_only", False):
                    u = s.get("hls_url") or s.get("url") or ""
                    if u and "playlist.m3u8" in u:
                        hls_url = u
                        break
            if not hls_url:
                hls = data2.get("hls_url", "")
                if hls and "playlist.m3u8" in hls:
                    hls_url = hls
            if hls_url:
                result["hls_url"] = hls_url
                push_log(f"stream URL: {hls_url[:100]}", "info")
    except Exception as e:
        push_log(f"play URL エラー: {e}", "warn")

    return result

def get_play_url(live_id: str, quality: str = "2high") -> str | None:
    """後方互換のため残す"""
    return get_live_data(live_id).get("hls_url")

def get_live_id_and_url_via_streamlink(user_path: str, quality: str = "best") -> tuple[str | None, str | None]:
    """streamlink フォールバック（未使用だが残す）"""
    return None, None

# ─── コメント収集 ────────────────────────────────────────

async def collect_comments(live_id: str, comment_file: Path, stop_event: asyncio.Event,
                            jwt: str = "", ws_url_base: str = "wss://ws.whowatch.tv/socket",
                            streamer: str = ""):
    """
    Phoenix Channels プロトコルでコメントを取得する。
    メッセージ形式: [join_ref, ref, topic, event, payload]
    """
    ws_url = f"{ws_url_base}/websocket?vsn=2.0.0"
    ws_headers = {
        "Origin": "https://whowatch.tv",
        "Referer": f"https://whowatch.tv/viewer/{live_id}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    topic = f"room:{live_id}"

    # JWT付きでjoin（APIから取得したトークンを使う）
    join_payload = {"p": jwt} if jwt else {}
    join_msg  = json.dumps(["1", "1", topic, "phx_join", join_payload])
    # Phoenix heartbeat (30秒ごとに送る)
    heartbeat = json.dumps([None, "hb", "phoenix", "heartbeat", {}])
    push_log(f"コメントWS: jwt={'あり' if jwt else 'なし'}", "info")

    try:
        push_log(f"コメントWS接続中: {ws_url}", "info")
        async with websockets.connect(ws_url, additional_headers=ws_headers, open_timeout=10, ping_interval=None) as ws:
            push_log(f"コメントWS接続成功 topic={topic}", "info")
            await ws.send(join_msg)

            last_hb = asyncio.get_event_loop().time()

            with open(comment_file, "a", encoding="utf-8") as fp:
                fp.write(f"# 配信開始: {datetime.datetime.now().isoformat()}\n")
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        # heartbeat送信
                        if asyncio.get_event_loop().time() - last_hb > 30:
                            await ws.send(heartbeat)
                            last_hb = asyncio.get_event_loop().time()
                        continue

                    try:
                        msg = json.loads(raw)
                        # Phoenix形式: [join_ref, ref, topic, event, payload]
                        if not isinstance(msg, list) or len(msg) < 5:
                            continue
                        _, _, _, event, payload = msg[0], msg[1], msg[2], msg[3], msg[4]

                        if event == "phx_reply":
                            push_log(f"WS join応答: {str(payload)[:100]}", "info")
                            continue
                        if event in ("phx_close", "phx_error"):
                            push_log(f"WS切断 event={event}", "warn")
                            break

                        # shout以外のイベントのみログ出力（shoutはコメント処理で表示）
                        if event not in ("phx_reply", "phx_close", "phx_error", "shout"):
                            push_log(f"WS event={event} payload={str(payload)[:200]}", "info")

                        if event in ("shout", "new_comment", "comment", "message", "play_item", "item", "gift"):
                            ts   = datetime.datetime.now().strftime("%H:%M:%S")
                            # shoutイベントのpayload構造:
                            # payload["comment"]["user"]["name"] / payload["comment"]["message"]
                            comment_obj = payload.get("comment") if isinstance(payload.get("comment"), dict) else payload
                            user_obj = comment_obj.get("user") if isinstance(comment_obj.get("user"), dict) else {}
                            user = str(user_obj.get("name")
                                    or comment_obj.get("user_name")
                                    or payload.get("user_name")
                                    or "???")
                            text = str(comment_obj.get("message")
                                    or comment_obj.get("escaped_message")
                                    or payload.get("message")
                                    or payload.get("text")
                                    or "")

                            # アイテム情報を取得
                            # shout payload構造: comment.play_item_pattern (辞書)
                            # comment.item_count (個数)
                            item_info = ""
                            pi = comment_obj.get("play_item_pattern")
                            if pi and isinstance(pi, dict):
                                item_name  = pi.get("name") or pi.get("item_name") or ""
                                item_count = comment_obj.get("item_count") or ""
                                if item_name:
                                    item_info = f" 🎁{item_name}" + (f"×{item_count}" if item_count and item_count != 1 else "")

                            if text or item_info:
                                display = (text + item_info).strip() if text else f"🎁{item_name}をプレゼント" + (f"×{item_count}" if item_count and item_count != 1 else "")
                                line = f"[{ts}] {user}: {display}"
                                fp.write(line + "\n")
                                fp.flush()
                                push_log(f"💬 [{user}] {display}", "comment")
                                # 匿名ユーザーはcomment.idで区別できないので
                                # user_nameとuser_pathの組み合わせをキーにする
                                sio.emit("comment", {
                                    "time": ts, "user": user, "text": display,
                                    "live_id": live_id,
                                    "user_id": user_obj.get("id", ""),
                                    "user_path": user_obj.get("user_path", ""),
                                    "comment_id": str(comment_obj.get("id", "")),
                                    "streamer": streamer,
                                })
                    except (json.JSONDecodeError, ValueError):
                        pass

    except Exception as e:
        push_log(f"コメントWS エラー: {e}", "warn")
    push_log(f"コメント収集終了 live_id={live_id}", "info")

# ─── 録画 ────────────────────────────────────────────────

def do_record(user_path: str, live_id: str, play_url: str, jwt: str = "", ws_url: str = "wss://ws.whowatch.tv/socket"):
    cfg = load_config()
    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    # 配信タイトルと配信者表示名を取得
    try:
        import time as _t
        r2 = requests.get(f"{API_BASE}/lives/{live_id}?last_updated_at={int(_t.time()*1000)}",
                          headers=HEADERS, timeout=8)
        if r2.status_code == 200:
            ldata = r2.json().get("live", {})
            title    = ldata.get("title", "")
            username = ldata.get("user", {}).get("name", "") or user_path
        else:
            title, username = "", user_path
    except Exception as _e:
        push_log(f"タイトル取得エラー: {_e}", "warn")
        title, username = "", user_path

    push_log(f"ファイル名情報: username={username} title={title[:20] if title else '(なし)'}", "info")

    def safe_name(s, maxlen=30):
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", s)   # Windowsで使えない文字を除去
        s = re.sub(r"\s+", "_", s.strip())
        return s[:maxlen]

    u = safe_name(username) or re.sub(r"[^\w\-]", "_", user_path)
    t = safe_name(title) if title else ""
    dt = now_str()
    stem = f"{u}_{t}_{dt}" if t else f"{u}_{dt}"
    base = out / stem
    video_file   = base.with_suffix(".ts")
    comment_file = base.with_suffix(".txt")

    push_log(f"▶ 録画開始: {user_path} → {video_file.name}", "start")

    ffmpeg = "ffmpeg"
    if sys.platform == "win32":
        # 1. 同じフォルダのffmpeg.exeを優先
        local_ff = BASE_DIR / "ffmpeg.exe"
        if local_ff.exists():
            ffmpeg = str(local_ff)
        else:
            # 2. Cドライブ直下、Downloadsなど一般的な場所を探す
            for candidate in [
                Path("C:/ffmpeg/bin/ffmpeg.exe"),
                Path("C:/ffmpeg/ffmpeg.exe"),
                Path.home() / "Downloads" / "ffmpeg.exe",
                Path.home() / "ffmpeg" / "ffmpeg.exe",
            ]:
                if candidate.exists():
                    ffmpeg = str(candidate)
                    break

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning",
           "-i", play_url, "-c", "copy", "-f", "mpegts", str(video_file)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    with _lock:
        recording_sessions[user_path] = {
            "live_id":    live_id,
            "proc":       proc,
            "start_time": now_str("%H:%M:%S"),
            "video_file": str(video_file),
        }
    push_status()

    # コメント収集（自動再接続ループ付き）
    stop_ev = asyncio.Event()
    if cfg.get("save_comments", True):
        def run_ws():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                while not stop_ev.is_set():
                    try:
                        # JWTを再取得してから再接続
                        current_jwt = jwt
                        current_ws  = ws_url
                        try:
                            fresh = get_live_data(live_id)
                            if fresh.get("jwt"):
                                current_jwt = fresh["jwt"]
                            if fresh.get("ws_url"):
                                current_ws  = fresh["ws_url"]
                        except Exception:
                            pass
                        loop.run_until_complete(
                            collect_comments(live_id, comment_file, stop_ev, current_jwt, current_ws, user_path)
                        )
                    except Exception as e:
                        push_log(f"コメントWS再接続エラー: {e}", "warn")
                    if not stop_ev.is_set():
                        push_log("コメントWS 5秒後に再接続...", "info")
                        time.sleep(5)
            finally:
                loop.close()
        threading.Thread(target=run_ws, daemon=True).start()

    proc.wait()
    stop_ev.set()

    with _lock:
        recording_sessions.pop(user_path, None)

    push_log(f"■ 録画終了: {user_path}", "stop")

    # コメントtxtにリスナー別アイテム集計を追記
    _append_item_summary(comment_file)

    # コメントをSRTに変換して動画に埋め込む（字幕埋め込みが先）
    if cfg.get("embed_subtitles", False):
        _embed_subtitles(video_file, comment_file, ffmpeg)

    # MEGAへアップロード（字幕埋め込み後に実行）
    if cfg.get("mega_upload", False):
        mfolder = cfg.get("mega_folder", "/whowatch")
        # txtをアップロード
        _upload_to_mega(comment_file, mfolder)
        # MP4をアップロード
        sub_file = video_file.with_stem(video_file.stem + "_sub").with_suffix(".mp4")
        if sub_file.exists():
            _upload_to_mega(sub_file, mfolder)
        elif video_file.exists():
            _upload_to_mega(video_file, mfolder)

    push_status()


def _get_mega_cmd(cmd_name: str = "mega-put") -> str:
    """MEGAcmdのパスを返す。見つからない場合はコマンド名をそのまま返す。"""
    import os as _os
    localappdata = _os.environ.get("LOCALAPPDATA", "")
    username = _os.environ.get("USERNAME", "")
    candidates = [
        Path(localappdata) / "MEGAcmd" / f"{cmd_name}.bat",
        Path(localappdata) / "MEGAcmd" / f"{cmd_name}.exe",
        Path(f"C:/Users/{username}/AppData/Local/MEGAcmd") / f"{cmd_name}.bat",
        Path(f"C:/Users/{username}/AppData/Local/MEGAcmd") / f"{cmd_name}.exe",
        Path("C:/Program Files/MEGAcmd") / f"{cmd_name}.bat",
        Path("C:/Program Files/MEGAcmd") / f"{cmd_name}.exe",
    ]
    for c in candidates:
        if c.exists():
            push_log(f"MEGAcmd発見: {c}", "info")
            return str(c)
    # PATHから探す
    push_log(f"MEGAcmd({cmd_name})をPATHから使用", "info")
    return cmd_name


def _upload_to_mega(file_path: Path, mega_folder: str = "/whowatch"):
    """MEGAcmdを使ってファイルをアップロードする。"""
    import subprocess as _sp
    try:
        mega_put   = _get_mega_cmd("mega-put")
        mega_mkdir = _get_mega_cmd("mega-mkdir")

        if not mega_put:
            push_log("mega-putが見つかりません", "warn")
            return

        # フォルダパスを正規化（空の場合はデフォルト）
        if not mega_folder or mega_folder.strip() == "":
            mega_folder = "/whowatch"
        folder = "/" + mega_folder.strip("/")
        push_log(f"MEGAフォルダ: {folder}", "info")

        # フォルダを作成
        _sp.run([mega_mkdir, "-p", folder],
                capture_output=True, text=True, timeout=30)

        push_log(f"MEGAアップロード開始: {file_path.name}", "info")

        # mega-put ローカルパス リモートフォルダパス
        # フォルダの末尾に / をつけることでフォルダ内に入れる
        result = _sp.run(
            [mega_put, str(file_path), folder + "/"],
            capture_output=True, text=True, timeout=7200
        )
        out = (result.stdout + result.stderr).strip()
        if result.returncode == 0 or "100.00 %" in out:
            push_log(f"MEGAアップロード完了: {file_path.name}", "info")
        else:
            push_log(f"MEGAアップロード失敗: {out[:150]}", "warn")
    except Exception as e:
        push_log(f"MEGAアップロードエラー: {e}", "warn")


def _append_item_summary(comment_file: Path):
    """コメントtxtからアイテム集計を生成して末尾に追記する。"""
    import re as _re
    item_stats: dict = {}
    try:
        lines = comment_file.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if "🎁" not in line:
                continue
            # 行形式: [HH:MM:SS] ユーザー名: テキスト 🎁アイテム名×個数
            parts = line.split("] ", 1)
            if len(parts) < 2:
                continue
            rest = parts[1]
            user_text = rest.split(": ", 1)
            if len(user_text) < 2:
                continue
            user = user_text[0]
            text = user_text[1]
            # 🎁以降を取得
            gift_idx = text.find("🎁")
            if gift_idx < 0:
                continue
            gift_part = text[gift_idx+1:]
            # "アイテム名×N" 形式を解析
            for m in _re.finditer(r"([^\s×,🎁]+)(?:×(\d+))?", gift_part):
                iname = m.group(1).strip()
                icount = int(m.group(2)) if m.group(2) else 1
                if not iname:
                    continue
                if user not in item_stats:
                    item_stats[user] = {}
                item_stats[user][iname] = item_stats[user].get(iname, 0) + icount
        if item_stats:
            with open(comment_file, "a", encoding="utf-8") as f:
                f.write("\n# -- アイテム集計 --\n")
                for user, items in sorted(item_stats.items(),
                                          key=lambda x: sum(x[1].values()), reverse=True):
                    total = sum(items.values())
                    detail = "  ".join(f"{k}x{v}" for k, v in items.items())
                    f.write(f"# {user}: {detail}  (合計{total}個)\n")
    except Exception as e:
        pass  # 集計失敗は無視


def _embed_subtitles(video_file: Path, comment_file: Path, ffmpeg_bin: str):
    """コメントtxtをSRTに変換して動画に字幕として埋め込む。"""
    import re as _re
    srt_file = video_file.with_suffix(".srt")
    out_file  = video_file.with_stem(video_file.stem + "_sub").with_suffix(".mp4")
    try:
        lines = comment_file.read_text(encoding="utf-8").splitlines()
        srt_lines = []
        idx = 1

        # 録画開始時刻をtxtの1行目から取得
        # 形式: # 配信開始: 2026-05-21T15:48:27.316798
        rec_start_ms = None
        for line in lines:
            ms = _re.match(r"# 配信開始: \d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})", line)
            if ms:
                h0, m0, s0 = int(ms.group(1)), int(ms.group(2)), int(ms.group(3))
                rec_start_ms = (h0 * 3600 + m0 * 60 + s0) * 1000
                break

        for line in lines:
            m = _re.match(r"\[(\d{2}:\d{2}:\d{2})\] (.+?):\s*(.*)", line)
            if not m:
                continue
            t_str, user, text = m.group(1), m.group(2), m.group(3)
            if not text:
                continue
            h, mn, s = map(int, t_str.split(":"))
            abs_ms = (h * 3600 + mn * 60 + s) * 1000
            # 録画開始時刻からの相対時間に変換
            if rec_start_ms is not None:
                start_ms = abs_ms - rec_start_ms
                if start_ms < 0:
                    start_ms += 86400 * 1000  # 日をまたいだ場合
            else:
                start_ms = abs_ms
            end_ms = start_ms + 3000
            def ms_to_srt(ms):
                hh = ms // 3600000; ms %= 3600000
                mm = ms // 60000;   ms %= 60000
                ss = ms // 1000;    ms %= 1000
                return f"{hh:02}:{mm:02}:{ss:02},{ms:03}"
            srt_lines.append(str(idx))
            srt_lines.append(f"{ms_to_srt(start_ms)} --> {ms_to_srt(end_ms)}")
            srt_lines.append(f"{user}: {text}")
            srt_lines.append("")
            idx += 1
        if not srt_lines:
            return
        srt_file.write_text("\n".join(srt_lines), encoding="utf-8")
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "warning",
               "-i", str(video_file), "-i", str(srt_file),
               "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=jpn", str(out_file)]
        subprocess.run(cmd, timeout=300)
        # 中間ファイルのSRTを削除
        srt_file.unlink(missing_ok=True)
        push_log(f"字幕埋め込み完了: {out_file.name}", "info")
        # TSファイルを削除
        video_file.unlink(missing_ok=True)
    except Exception as e:
        push_log(f"字幕埋め込みエラー: {e}", "warn")

def start_recording(user_path: str):
    user_path = normalize_user_path(user_path)
    with _lock:
        if user_path in recording_sessions:
            return
    cfg = load_config()

    live_id = get_live_id(user_path)
    if not live_id:
        push_log(f"配信中ではありません: {user_path}", "info")
        return

    live_data = get_live_data(live_id)
    play_url = live_data.get("hls_url")
    jwt = live_data.get("jwt", "")
    ws_url = live_data.get("ws_url", "wss://ws.whowatch.tv/socket")

    if not play_url:
        push_log(f"play URL 取得失敗: {user_path}", "warn")
        return

    t = threading.Thread(target=do_record, args=(user_path, live_id, play_url, jwt, ws_url), daemon=True)
    t.start()

def stop_recording(user_path: str):
    with _lock:
        s = recording_sessions.get(user_path)
    if s:
        try:
            s["proc"].terminate()
        except Exception:
            pass
        push_log(f"⏹ 手動停止: {user_path}", "stop")

# ─── 監視ループ ──────────────────────────────────────────
_monitor_running = False

def monitor_loop():
    global _monitor_running
    _monitor_running = True
    push_log("監視ループ 開始", "info")
    while _monitor_running:
        cfg = load_config()
        # /lives を1回だけ取得してキャッシュを更新
        global _lives_cache_time
        _lives_cache_time = 0  # キャッシュ強制リフレッシュ
        _fetch_lives()
        for up in cfg.get("streamers", []):
            normalized = normalize_user_path(up)
            with _lock:
                already = normalized in recording_sessions
            if not already:
                live_id = get_live_id(normalized)
                if live_id:
                    start_recording(normalized)
        time.sleep(cfg.get("check_interval", 30))
    push_log("監視ループ 停止", "info")

_monitor_thread: threading.Thread | None = None

def start_monitor():
    global _monitor_thread, _monitor_running
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_running = True
    _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    _monitor_thread.start()
    push_log("✅ 自動監視を開始しました", "info")
    push_status()

def stop_monitor():
    global _monitor_running
    _monitor_running = False
    push_log("⏸ 自動監視を停止しました", "info")
    push_status()

# ─── Flask ルート ─────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/api/status")
def api_status():
    return jsonify(build_status())

@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    start_monitor()
    return jsonify({"ok": True})

@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    stop_monitor()
    return jsonify({"ok": True})

@app.route("/api/streamer/add", methods=["POST"])
def api_streamer_add():
    raw = (request.json or {}).get("user_path", "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "user_path が空です"})
    user_path = normalize_user_path(raw)
    cfg = load_config()
    if user_path in cfg["streamers"]:
        return jsonify({"ok": False, "error": f"既に登録済みです: {user_path}"})
    cfg["streamers"].append(user_path)
    save_config(cfg)
    push_log(f"配信者追加: {user_path}", "info")
    push_status()
    return jsonify({"ok": True, "user_path": user_path})

@app.route("/api/streamer/remove", methods=["POST"])
def api_streamer_remove():
    user_path = (request.json or {}).get("user_path", "").strip()
    cfg = load_config()
    if user_path in cfg["streamers"]:
        cfg["streamers"].remove(user_path)
        save_config(cfg)
        push_log(f"配信者削除: {user_path}", "info")
        push_status()
    return jsonify({"ok": True})

@app.route("/api/streamer/test", methods=["POST"])
def api_streamer_test():
    user_path = normalize_user_path((request.json or {}).get("user_path", "").strip())
    global _lives_cache_time
    _lives_cache_time = 0  # キャッシュを強制リフレッシュ
    live_id = get_live_id(user_path)
    if live_id:
        push_log(f"テスト: {user_path} -> 配信中 (live_id={live_id})", "start")
        return jsonify({"ok": True, "live": True, "live_id": live_id})
    push_log(f"テスト: {user_path} -> オフライン", "info")
    return jsonify({"ok": True, "live": False})

@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    user_path = (request.json or {}).get("user_path", "").strip()
    threading.Thread(target=start_recording, args=(user_path,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    user_path = (request.json or {}).get("user_path", "").strip()
    stop_recording(user_path)
    return jsonify({"ok": True})

@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    data = request.json or {}
    cfg = load_config()
    if "check_interval" in data:
        cfg["check_interval"] = int(data["check_interval"])
    if "quality" in data:
        cfg["quality"] = data["quality"]
    if "save_comments" in data:
        cfg["save_comments"] = bool(data["save_comments"])
    if "output_dir" in data:
        cfg["output_dir"] = data["output_dir"]
    if "embed_subtitles" in data:
        cfg["embed_subtitles"] = bool(data["embed_subtitles"])
    if "whowatch_cookie" in data:
        cfg["whowatch_cookie"] = str(data["whowatch_cookie"])
    if "whowatch_device_id" in data:
        cfg["whowatch_device_id"] = str(data["whowatch_device_id"])
    if "mega_upload" in data:
        cfg["mega_upload"] = bool(data["mega_upload"])
    if "mega_folder" in data:
        cfg["mega_folder"] = str(data["mega_folder"])
    save_config(cfg)
    push_log(f"設定を保存しました (字幕埋め込み={'ON' if cfg.get('embed_subtitles') else 'OFF'})", "info")
    push_status()
    return jsonify({"ok": True, "config": cfg})

@app.route("/api/browse_folder", methods=["POST"])
def api_browse_folder():
    import subprocess as _sp, sys as _sys
    try:
        if _sys.platform == "win32":
            script = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
                '$d.Description="保存フォルダを選択してください";'
                'if($d.ShowDialog()-eq"OK"){$d.SelectedPath}else{""}')
            result = _sp.run(["powershell","-NoProfile","-Command",script],
                             capture_output=True, text=True, timeout=60)
            path = result.stdout.strip()
            if path:
                return jsonify({"path": path})
        return jsonify({"path": ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    cfg = load_config()
    folder = Path(cfg["output_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(folder))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return jsonify({"ok": True})

@app.route("/api/logs")
def api_logs():
    return jsonify(log_buffer[-200:])

@sio.on("connect")
def on_connect():
    emit("status", build_status())
    for entry in log_buffer[-100:]:
        emit("log", entry)

# ─── メイン ──────────────────────────────────────────────

if __name__ == "__main__":
    STATIC_DIR.mkdir(exist_ok=True)
    push_log("ふわっち自動録画アプリ 起動中...", "info")

    # 少し待ってからブラウザを開く
    def open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:5000")
    threading.Thread(target=open_browser, daemon=True).start()

    sio.run(app, host="127.0.0.1", port=5000, debug=False, use_reloader=False)
