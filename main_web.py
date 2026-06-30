import os
import sys
import json
import time
import sqlite3
import asyncio
import threading
import urllib.request
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from telethon import TelegramClient, events

# Windows 下 Telethon + asyncio 更稳定的事件循环策略
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 自动创建用于存放 TG 账号文件的 sessions 文件夹
os.makedirs("sessions", exist_ok=True)
templates = Jinja2Templates(directory="templates")

# Windows 下默认控制台常为 GBK，遇到 emoji 会触发 UnicodeEncodeError
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 全局 TG 配置（支持环境变量，便于服务器部署）====================
API_ID = int(os.environ.get("TG_API_ID", "2040"))
API_HASH = os.environ.get("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")
DB_FILE = os.environ.get("DB_FILE", "tg_cloud_stats.db")

# 🤖 通知机器人核心配置 (用于向你的个人 TG 推送通知)
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8748068830:AAFnHnzjcBlNQXagAyDlLabCq4o51W_aRCY")
ADMIN_CHAT_ID = int(os.environ.get("TG_ADMIN_CHAT_ID", "5654372746"))


def create_telegram_client(session_path: str) -> TelegramClient:
    """直连 Telegram（服务器可正常出海时无需代理）"""
    return TelegramClient(session_path, API_ID, API_HASH)

# 全局字典：记录当前正在后台【真正运行】监听的客户端实例 {session_name: client_instance}
running_clients = {}
# 正在启动中的 session，防止重复 /activate 并发打开同一 .session 文件
_starting_sessions: set[str] = set()
_session_lifecycle_lock = threading.Lock()
# 全局字典：用于临时存储手机号验证码登录流中的中间连接数据
login_cache = {}
# 中央统计通知 Bot（Telethon，处理白名单与大盘查询）
notification_bot = None
# 专用 Telethon 后台线程事件循环，避免阻塞 Web 主循环
_tg_loop = None
_tg_loop_ready = threading.Event()

# ==================== SQLite3 数据库操作 ====================
def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def should_skip_matrix_session(session_name: str) -> bool:
    """sessions/ 目录里只放矩阵受控主号，跳过 Bot/通知类 session 避免文件锁冲突"""
    lowered = session_name.lower()
    return "bot" in lowered or "notification" in lowered


def _start_tg_event_loop():
    """在独立守护线程中运行 Telethon 专用事件循环"""
    global _tg_loop

    def _runner():
        global _tg_loop
        _tg_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_tg_loop)
        _tg_loop_ready.set()
        _tg_loop.run_forever()

    threading.Thread(target=_runner, name="tg-event-loop", daemon=True).start()
    if not _tg_loop_ready.wait(timeout=10):
        raise RuntimeError("Telethon 后台事件循环启动超时")


def ensure_tg_loop():
    if _tg_loop is None:
        _start_tg_event_loop()
    return _tg_loop


def schedule_on_tg_loop(coro):
    """把协程投递到 Telethon 专用线程执行"""
    loop = ensure_tg_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


async def run_on_tg_loop(coro):
    """在 Web 请求中等待 Telethon 线程执行结果"""
    return await asyncio.wrap_future(schedule_on_tg_loop(coro))


def init_db():
    """初始化 SQLite3 数据库表结构"""
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS strangers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            created_at TEXT,
            UNIQUE(session_name, user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT
        )
    ''')
    cursor.execute("DELETE FROM strangers WHERE session_name LIKE '%bot%'")
    conn.commit()
    conn.close()
    print("🗄️ SQLite3 数据库初始化/检查并物理清洗完成。")


def is_authorized_user(user_id: int) -> bool:
    if user_id == ADMIN_CHAT_ID:
        return True
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None


def add_to_whitelist(user_id: int) -> bool:
    conn = _db_connect()
    cursor = conn.cursor()
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "INSERT OR IGNORE INTO whitelist (user_id, added_at) VALUES (?, ?)",
            (user_id, now),
        )
        conn.commit()
        if cursor.rowcount > 0:
            return True
        cursor.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


def remove_from_whitelist(user_id: int) -> None:
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_whitelist() -> list[int]:
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM whitelist ORDER BY added_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def delete_session_records(session_name: str) -> int:
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM strangers WHERE session_name = ?", (session_name,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_notification_recipients() -> list[int]:
    return list({ADMIN_CHAT_ID, *get_whitelist()})

def get_stats():
    """从数据库获取前端大盘需要渲染的所有历史数据"""
    conn = _db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT session_name, user_id, username, full_name, created_at FROM strangers ORDER BY id DESC")
    rows = cursor.fetchall()
    cursor.execute("SELECT session_name, COUNT(*) FROM strangers GROUP BY session_name")
    counts = dict(cursor.fetchall())
    conn.close()
    
    logs = []
    for r in rows:
        logs.append({
            "session": r[0], "user_id": r[1], "username": r[2],
            "full_name": r[3], "created_at": r[4]
        })
    return logs, counts


def get_matrix_counts(counts: dict) -> dict[str, int]:
    return {k: v for k, v in counts.items() if "bot" not in k.lower()}


def list_matrix_session_names() -> list[str]:
    """扫描 sessions 目录，返回所有矩阵受控号（不含 Bot 类 session）"""
    if not os.path.isdir("sessions"):
        return []
    return sorted(
        f.replace(".session", "")
        for f in os.listdir("sessions")
        if f.endswith(".session") and not should_skip_matrix_session(f.replace(".session", ""))
    )


def build_matrix_counts_display(counts: dict) -> dict[str, int]:
    """合并本地 session 与数据库计数，无进粉记录的监控号也显示为 0"""
    db_counts = get_matrix_counts(counts)
    display = {name: db_counts.get(name, 0) for name in list_matrix_session_names()}
    for name, count in db_counts.items():
        if name not in display:
            display[name] = count
    return display


def format_stats_digest() -> str:
    """拼装各监控号进粉累计 + 最新名片（Bot 与授权用户查询共用）"""
    logs, counts = get_stats()
    matrix_counts = build_matrix_counts_display(counts)
    total = sum(matrix_counts.values())

    reply_text = "📊 <b>TG 分布式云控实时大盘简报</b>\n\n"
    reply_text += f"<b>📈 全矩阵累计进粉：</b> <b>{total}</b> 人\n\n"
    reply_text += "<b>🟢 各监控号进粉累计：</b>\n"

    if not matrix_counts:
        reply_text += "  暂无任何受控监控号。\n"
    else:
        for session_name, count in sorted(matrix_counts.items(), key=lambda x: (-x[1], x[0])):
            reply_text += f"  • <code>{session_name}</code>: <b>{count}</b> 人\n"

    reply_text += "\n📋 <b>最新捕获的新粉名片：</b>\n"
    recent_logs = logs[:5]
    if not recent_logs:
        reply_text += "  鱼塘暂时还没有动静...\n"
    else:
        for idx, log in enumerate(recent_logs, 1):
            reply_text += (
                f"  {idx}. 【{log['session']}】新增粉丝：<b>{log['full_name']}</b> ({log['username']})\n"
            )

    reply_text += f"\n⏰ <i>更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"
    return reply_text

def save_stranger(session_name, user_id, username, full_name):
    """尝试将捕获到的陌生人写入数据库，重复消息自动清洗去重"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for attempt in range(5):
        conn = _db_connect()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO strangers (session_name, user_id, username, full_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_name, user_id, username, full_name, now)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
        finally:
            conn.close()
    return False

# ==================== TG 机器人推送逻辑 ====================
def _send_bot_message_sync(chat_id: int, text: str) -> bool:
    """通过 Telegram Bot HTTP API 推送消息"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status == 200


async def push_notification(session_name, full_name, user_id, username):
    """向管理员及白名单用户推送实时进粉通知"""
    matrix_counts = get_matrix_counts((await asyncio.to_thread(get_stats))[1])
    session_total = matrix_counts.get(session_name, 0)
    text = (
        f"🎯 <b>系统实时进粉通知</b>\n\n"
        f"<b>📁 进粉受控主号：</b> <code>{session_name}</code>\n"
        f"<b>👤 粉丝昵称：</b> {full_name}\n"
        f"<b>🆔 粉丝唯一ID：</b> <code>{user_id}</code>\n"
        f"<b>🔗 跳转链接：</b> {username}\n"
        f"<b>📊 该号累计进粉：</b> <b>{session_total}</b> 人\n"
        f"<b>⏰ 捕获时间：</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    recipients = await asyncio.to_thread(get_notification_recipients)
    for chat_id in recipients:
        try:
            ok = await asyncio.to_thread(_send_bot_message_sync, chat_id, text)
            if ok:
                print(f"🚀 [Bot] 已向 [{chat_id}] 推送 [{session_name}] 进粉通知。")
        except Exception as e:
            print(f"🚨 [Bot] 向 [{chat_id}] 推送失败: {e}")

# ==================== TG 矩阵监听机制 ====================
async def stranger_msg_handler(event, session_name):
    """
    【受控号专属拦截器】
    只允许 sessions/ 目录下的受控矩阵主号触发。不参与记录具体的聊天文本内容。
    只抓取是谁发来的（名字/ID/链接），并利用 SQLite 物理去重，实现精准进粉计数。
    """
    # 🛑 铁律 1：如果传入的 session 名字包含机器人标识，坚决拒绝入库，防止张冠李戴
    if "bot" in session_name.lower():
        return

    if not event.is_private: 
        return  # 🛑 铁律 2：过滤掉所有群组消息，只监控私聊主动进粉
        
    try:
        sender = await event.get_sender()
        if not sender:
            sender = await event.client.get_entity(event.peer_id)
            
        if not sender or sender.is_self or sender.bot:
            return  # 排除自己发的以及任何其它官方/业务机器人

        # 提取粉丝基础身份信息（具体发了什么内容 event.raw_text 在这里直接被无视，保护隐私不留存）
        username = f"@{sender.username}" if sender.username else "未设置用户名"
        full_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        
        # 打印原始拦截心跳（控制台可见，用来证明链路是活的）
        print(f"🔔 矩阵受控号 [{session_name}] 侦测到陌生人 [{full_name}] 传来私聊信号。")

        # 直接尝试写入 SQLite 数据库（UNIQUE 索引会自动判断该粉丝是否是该受控号的新粉丝）
        if await asyncio.to_thread(save_stranger, session_name, sender.id, username, full_name):
            # 进粉成功！
            print(f"🎯 [新粉入网] 矩阵号 [{session_name}] 成功在 SQLite 记录全新新进粉丝: {full_name} ({username})，已计入大盘！")
            
            # 让中央统计机器人去干它的本职工作：推送统计名片
            schedule_on_tg_loop(push_notification(session_name, full_name, sender.id, username))
        else:
            # 如果之前已经记录过这个人，说明他只是在继续聊天，不重复计算进粉数
            print(f"ℹ️ [日常互动] 矩阵号 [{session_name}] 粉丝 {full_name} 属于老客日常互动，不重复累加计数。")
                
    except Exception as e:
        print(f"🚨 消息拦截器局部异常: {str(e)}")

def start_msg_listener(client, session_name):
    """强行将消息接收事件挂载到 Telethon 核心事件队列中"""
    print(f"📡 正在为矩阵主号 [{session_name}] 强行注入事件拦截网...")
    
    # 通过显式传入默认参数(s_name=session_name)，锁死多账号并发下的异步上下文作用域，防止串号
    client.add_event_handler(
        lambda event, s_name=session_name: stranger_msg_handler(event, s_name), 
        events.NewMessage(incoming=True)
    )

async def run_tg_listener(session_path, session_name):
    """用于加载离线卡片或启动时批量恢复现有的受控矩阵账号文件"""
    with _session_lifecycle_lock:
        if session_name in running_clients or session_name in _starting_sessions:
            print(f"⏭️ 账号 [{session_name}] 已在运行或启动中，跳过重复加载。")
            return
        _starting_sessions.add(session_name)

    print(f"🔄 后端建立网络连接，正在安全加载受控 Session: {session_name}...")
    client = create_telegram_client(session_path)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"❌ 警告：Session [{session_name}] 离线或授权已失效，跳过。")
            await client.disconnect()
            return

        with _session_lifecycle_lock:
            running_clients[session_name] = client
        start_msg_listener(client, session_name)
        me = await client.get_me()
        print(f"🟢 矩阵受控号监听完全激活: {session_name} ([@{me.username or '无'}])")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"🚨 账号 [{session_name}] 后台任务发生异常退出! 原因: {str(e)}")
    finally:
        with _session_lifecycle_lock:
            _starting_sessions.discard(session_name)
            if running_clients.get(session_name) is client:
                del running_clients[session_name]
        try:
            await client.disconnect()
        except Exception:
            pass

async def background_keep_alive(client, session_name):
    """专门用来接管手动登录成功后的 client 实例，在后台维持死循环长连接"""
    try:
        print(f"🔁 账号 [{session_name}] 已成功挂载，进入常驻后台长连接监听态...")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"🚨 账号 [{session_name}] 后台断开: {str(e)}")
    finally:
        if session_name in running_clients: 
            del running_clients[session_name]
        try: 
            await client.disconnect()
        except: 
            pass
        print(f"ℹ️ 账号 [{session_name}] 后台维持任务正常结束。")

# ==================== 服务自动启动恢复任务 ====================
async def auto_load_all_sessions():
    """机器人自动批量拉起：扫描本地现有 sessions 并批量恢复监控"""
    print("🤖 启动全自动批量监控任务，正在检索本地受控主号 session 矩阵...")
    if not os.path.exists("sessions"):
        return
    
    files = [f for f in os.listdir("sessions") if f.endswith(".session")]
    if not files:
        print("📂 暂未发现本地缓存的受控主号 session 文件，等待管理员后续手动添加。")
        return
        
    matrix_files = []
    for file in files:
        session_name = file.replace(".session", "")
        if should_skip_matrix_session(session_name):
            print(f"⏭️ 跳过非矩阵受控号 Session: {session_name}")
            continue
        matrix_files.append((file, session_name))

    if not matrix_files:
        print("📂 未发现可加载的矩阵受控主号 session 文件。")
        return

    print(f"📂 发现本地共有 {len(matrix_files)} 个受控账号文件，开始进行并发拓扑连接...")
    for file, session_name in matrix_files:
        file_path = f"sessions/{file}"
        schedule_on_tg_loop(run_tg_listener(file_path, session_name))

async def logout_and_remove_session(session_name: str) -> dict:
    """断开连接、删除 session 文件及该号全部进粉记录"""
    if should_skip_matrix_session(session_name):
        raise ValueError("不能操作 Bot 类 Session")

    client = None
    with _session_lifecycle_lock:
        client = running_clients.pop(session_name, None)
        _starting_sessions.discard(session_name)

    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

    removed_files = []
    sessions_dir = "sessions"
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if fname.startswith(f"{session_name}.session"):
                fpath = os.path.join(sessions_dir, fname)
                try:
                    os.remove(fpath)
                    removed_files.append(fname)
                except OSError as e:
                    print(f"⚠️ 删除文件失败 {fpath}: {e}")

    deleted_rows = await asyncio.to_thread(delete_session_records, session_name)
    print(f"🗑️ 已移除账号 [{session_name}]，删除文件 {removed_files}，清除 {deleted_rows} 条进粉记录。")
    return {"session_name": session_name, "files": removed_files, "deleted_records": deleted_rows}


async def start_notification_bot():
    """启动中央统计 Bot：白名单管理 + 授权用户查询大盘"""
    global notification_bot
    print("🤖 正在初始化中央常驻后台推送通知机器人...")
    bot_session_path = "sessions/bot_notification_service"
    notification_bot = create_telegram_client(bot_session_path)
    try:
        await notification_bot.start(bot_token=BOT_TOKEN)
        bot_me = await notification_bot.get_me()
        print(f"🟢 中央统计机器人激活成功! 推送官: [@{bot_me.username}]")

        @notification_bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def bot_interactive_handler(event):
            sender_id = event.sender_id
            text = (event.raw_text or "").strip()

            if sender_id == ADMIN_CHAT_ID:
                if text.startswith("/add "):
                    try:
                        target_id = int(text.split(" ", 1)[1].strip())
                        if add_to_whitelist(target_id):
                            await event.reply(
                                f"✅ 授权成功！用户 <code>{target_id}</code> 已加入白名单。",
                                parse_mode="html",
                            )
                        else:
                            await event.reply("ℹ️ 该用户已在白名单中，或操作未生效。")
                    except (ValueError, IndexError):
                        await event.reply(
                            "⚠️ 格式错误，正确格式：<code>/add 123456789</code>",
                            parse_mode="html",
                        )
                    return

                if text.startswith("/del "):
                    try:
                        target_id = int(text.split(" ", 1)[1].strip())
                        remove_from_whitelist(target_id)
                        await event.reply(
                            f"🗑️ 已取消用户 <code>{target_id}</code> 的白名单授权。",
                            parse_mode="html",
                        )
                    except (ValueError, IndexError):
                        await event.reply(
                            "⚠️ 格式错误，正确格式：<code>/del 123456789</code>",
                            parse_mode="html",
                        )
                    return

                if text == "/list":
                    w_list = get_whitelist()
                    if not w_list:
                        await event.reply("📂 当前动态白名单为空。")
                    else:
                        reply = "📋 <b>当前受信任白名单用户列表：</b>\n\n"
                        for idx, uid in enumerate(w_list, 1):
                            reply += f"  {idx}. <code>{uid}</code>\n"
                        reply += "\nℹ️ <i>发送 <code>/del 用户ID</code> 可移除授权。</i>"
                        await event.reply(reply, parse_mode="html")
                    return

                if text in ("/help", "/start"):
                    await event.reply(
                        "🤖 <b>管理员指令</b>\n"
                        "<code>/add 用户ID</code> — 授权查询大盘\n"
                        "<code>/del 用户ID</code> — 取消授权\n"
                        "<code>/list</code> — 查看白名单\n"
                        "发送任意消息 — 查看实时进粉大盘",
                        parse_mode="html",
                    )
                    return

            if not is_authorized_user(sender_id):
                await event.reply(
                    "❌ 您没有权限查询此系统。\n请联系管理员为您开通授权（向 Bot 发送您的 Telegram 用户 ID）。"
                )
                return

            reply_text = await asyncio.to_thread(format_stats_digest)
            await event.reply(reply_text, parse_mode="html")
            print(f"🤖 [Bot] 已向授权用户 [{sender_id}] 输出实时大盘简报。")

    except Exception as e:
        print(f"🚨 中央统计机器人拉起失败: {str(e)}")
        try:
            await notification_bot.disconnect()
        except Exception:
            pass
        notification_bot = None

async def bootstrap_tg_services():
    """后台拉起 TG 服务，避免阻塞 Web 页面首次响应"""
    ensure_tg_loop()
    await asyncio.sleep(0.5)
    await start_notification_bot()
    await auto_load_all_sessions()


# ==================== FastAPI 生命周期初始化 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 数据库初始化（放线程池，避免阻塞事件循环）
    await asyncio.to_thread(init_db)
    # 2/3. TG 监听与通知机器人在后台异步拉起，Web 页面可立即访问
    asyncio.create_task(bootstrap_tg_services())
    yield
    # 服务关闭时安全断开所有长连接
    print("🛑 正在关闭 Web 服务，正在释放所有 TG 后台长连接...")

    async def _shutdown_clients():
        global notification_bot
        for name, client in list(running_clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        if notification_bot is not None:
            try:
                await notification_bot.disconnect()
            except Exception:
                pass
            notification_bot = None

    if _tg_loop is not None:
        try:
            fut = schedule_on_tg_loop(_shutdown_clients())
            fut.result(timeout=10)
            _tg_loop.call_soon_threadsafe(_tg_loop.stop)
        except Exception:
            pass
    print("🛑 所有后台长连接已安全切断。")

app = FastAPI(lifespan=lifespan)

# ==================== Web 路由接口 ====================
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    logs, counts = await asyncio.to_thread(get_stats)
    matrix_counts = build_matrix_counts_display(counts)
    total_fans = sum(matrix_counts.values())
    existing_sessions = await asyncio.to_thread(list_matrix_session_names)
    active_sessions = list(running_clients.keys())

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "logs": logs,
            "counts": counts,
            "matrix_counts": matrix_counts,
            "total_fans": total_fans,
            "existing_sessions": existing_sessions,
            "active_sessions": active_sessions,
            "whitelist": await asyncio.to_thread(get_whitelist),
        },
    )

@app.post("/upload_session")
async def upload_session(file: UploadFile = File(...)):
    if not file.filename.endswith(".session"):
        return JSONResponse(status_code=400, content={"error": "请上传标准的 .session 后缀文件"})
    
    session_name = file.filename.replace(".session", "")
    file_path = f"sessions/{file.filename}"
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())

    schedule_on_tg_loop(run_tg_listener(file_path, session_name))
    return RedirectResponse(url="/", status_code=303)

# ----【AJAX 第一步：发送验证码】----
async def _login_step1_impl(phone: str):
    clean_phone = phone.replace('+', '').replace(' ', '').strip()
    session_name = f"manual_{clean_phone}"
    file_path = f"sessions/{session_name}.session"

    if clean_phone in login_cache:
        try:
            await login_cache[clean_phone]["client"].disconnect()
        except Exception:
            pass

    print(f"📱 正在为手机号 {phone} 创建全新的网络连接客户端...")
    client = create_telegram_client(file_path)

    try:
        await client.connect()
        if await client.is_user_authorized():
            print(f"ℹ️ 经检测，手机号 {phone} 本地已是在线登录状态，直接拉起。")
            running_clients[session_name] = client
            start_msg_listener(client, session_name)
            schedule_on_tg_loop(background_keep_alive(client, session_name))
            return {"status": "already_logged_in", "msg": "该账号本地已是在线登录状态，已直接拉起监听！"}

        send_code_result = await client.send_code_request(phone)
        login_cache[clean_phone] = {
            "client": client, "phone_code_hash": send_code_result.phone_code_hash,
            "phone": phone, "session_name": session_name, "file_path": file_path
        }
        print(f"📩 官方验证码已成功下发至手机号 {phone}。")
        return {"status": "success", "msg": "验证码下发成功！"}
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        print(f"❌ 发码失败: {e}")
        raise


@app.post("/login_step1")
async def login_step1(phone: str = Form(...)):
    try:
        result = await run_on_tg_loop(_login_step1_impl(phone))
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


async def _login_step2_impl(phone: str, code: str, password: str | None):
    clean_phone = phone.replace('+', '').replace(' ', '').strip()

    if clean_phone not in login_cache:
        raise ValueError("未找到对应的暂存通道链路，请重新尝试第一步发码。")

    cache = login_cache[clean_phone]
    client = cache["client"]
    session_name = cache["session_name"]

    print(f"🔑 正在向 TG 官方提交 5 位验证码 [{code}] 进行安全鉴权...")
    try:
        await client.sign_in(phone=cache["phone"], code=code, phone_code_hash=cache["phone_code_hash"])
    except Exception as e:
        if "password" in str(e).lower() or "protected" in str(e).lower() or "two-step" in str(e).lower():
            if password:
                print("🔐 侦测到两步验证保护！正在二次提交独立密码进行验证登录...")
                await client.sign_in(password=password)
            else:
                return {"error": "NEED_PASSWORD", "status_code": 400}
        else:
            raise e

    print(f"🎉 手机号 {phone} 顺利通关，登录成功！")
    running_clients[session_name] = client
    start_msg_listener(client, session_name)
    schedule_on_tg_loop(background_keep_alive(client, session_name))

    if clean_phone in login_cache:
        del login_cache[clean_phone]

    return {"status": "success", "msg": "登录成功，已在后台无锁状态激活长监听！"}


@app.post("/login_step2")
async def login_step2(phone: str = Form(...), code: str = Form(...), password: str = Form(None)):
    try:
        result = await run_on_tg_loop(_login_step2_impl(phone, code, password))
        if result.get("error") == "NEED_PASSWORD":
            return JSONResponse(status_code=400, content={"error": "NEED_PASSWORD"})
        return JSONResponse(content=result)
    except Exception as e:
        print(f"❌ 验证码或两步验证密码校验失败: {e}")
        return JSONResponse(status_code=400, content={"error": f"安全校验失败，登录终止: {str(e)}"})

# 手动激活离线账号
@app.get("/activate/{session_name}")
async def activate_session(session_name: str):
    if session_name in running_clients:
        return RedirectResponse(url="/", status_code=303)
    file_path = f"sessions/{session_name}.session"
    if os.path.exists(file_path):
        schedule_on_tg_loop(run_tg_listener(file_path, session_name))
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout/{session_name}")
async def logout_session(session_name: str):
    if should_skip_matrix_session(session_name):
        return JSONResponse(status_code=400, content={"error": "无效操作"})
    try:
        await run_on_tg_loop(logout_and_remove_session(session_name))
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    import uvicorn

    web_host = os.environ.get("WEB_HOST", "0.0.0.0")
    web_port = int(os.environ.get("WEB_PORT", "8006"))
    print(f"🚀 正在拉起 Uvicorn Web 服务 ({web_host}:{web_port})...")
    uvicorn.run("main_web:app", host=web_host, port=web_port, reload=False)