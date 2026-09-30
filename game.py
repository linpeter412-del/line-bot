"""每日 1A2B 猜數字遊戲的邏輯與儲存。

規則:
- 每天全部使用者共用同一組 4 位不重複的數字(0-9)。
- 使用者傳 4 位數字猜測,回覆 xAyB。
- 猜對即鎖定,當天不能再猜。
- 4 次以內猜對可獲得「出題資格」:下一則 4 位不重複數字會被排入未來某一天的題目。
- 猜對每日題目後會顯示當日排行榜(依次數排列),並可傳 next 玩個人練習題(不列入排行榜)。
"""

import logging
import os
import random
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from linebot.v3.messaging import ApiClient, BroadcastRequest, Configuration, MessagingApi, TextMessage

DB_PATH = Path(__file__).parent / "data" / "game.db"
TAIPEI = timezone(timedelta(hours=8))
logger = logging.getLogger("uvicorn.error")
GUESS_RE = re.compile(r"^[0-9]{4}$")
WIN_BONUS_ATTEMPTS = 4
LEADERBOARD_SIZE = 10

HELP_TEXT = (
    "🔢 每日猜數字(1A2B)\n"
    "每天大家共用同一組 4 位不重複的數字(0-9)。\n"
    "傳一組 4 位數字給我猜猜看,例如:1234\n\n"
    "回覆格式 xAyB:\n"
    "A = 數字對、位置也對\n"
    "B = 數字對、位置不對\n\n"
    "猜對就鎖定,明天會換新題目。\n"
    f"如果在 {WIN_BONUS_ATTEMPTS} 次以內猜對,你可以出一題給大家玩!\n"
    "猜對後可以傳「next」玩隨機練習題(不列入排行榜)。\n\n"
    "指令:\n"
    "「查詢」或「進度」— 看今天猜過的紀錄\n"
    "「排行榜」— 看今天的排行榜\n"
    "「next」— 猜對後玩下一題練習題\n"
    "「規則」— 顯示這段說明"
)

HELP_TRIGGERS = {"規則", "說明", "help", "Help", "幫助", "怎麼玩"}
PROGRESS_TRIGGERS = {"查詢", "紀錄", "進度"}
LEADERBOARD_TRIGGERS = {"排行榜", "排行", "排名"}
NEXT_TRIGGERS = {"next", "下一題"}
SKIP_TRIGGERS = {"跳過", "取消", "skip"}
ADMIN_REVEAL_TRIGGER = "成功了"
ADMIN_RESET_RE = re.compile(r"^(?:重置|重製)\s*([0-9]{4})?$")
RESET_ANNOUNCEMENT = "📢 管理員重置了今天的題目,大家可以重新開始猜囉!(之前的紀錄都已清空)"


def today_str() -> str:
    return datetime.now(TAIPEI).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(TAIPEI).isoformat()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_secret (
                date TEXT PRIMARY KEY,
                secret TEXT NOT NULL,
                set_by TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS future_secrets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                secret TEXT NOT NULL,
                set_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                used_date TEXT
            );

            CREATE TABLE IF NOT EXISTS user_day (
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                solved INTEGER NOT NULL DEFAULT 0,
                solved_at TEXT,
                PRIMARY KEY (user_id, date)
            );

            CREATE TABLE IF NOT EXISTS guess_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                guess TEXT NOT NULL,
                a INTEGER NOT NULL,
                b INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_state (
                user_id TEXT PRIMARY KEY,
                awaiting_secret INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bot_admin (
                user_id TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS practice_game (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                secret TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                solved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS practice_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                guess TEXT NOT NULL,
                a INTEGER NOT NULL,
                b INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def gen_random_secret() -> str:
    """隨機產生 4 位不重複的數字(0-9)。"""
    return "".join(random.sample("0123456789", 4))


def _has_unique_digits(text: str) -> bool:
    return len(set(text)) == len(text)


def compute_feedback(secret: str, guess: str) -> tuple[int, int]:
    a = sum(1 for s, g in zip(secret, guess) if s == g)
    common = sum((Counter(secret) & Counter(guess)).values())
    return a, common - a


def _pop_queued_secret(conn: sqlite3.Connection, date: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT id, secret, set_by FROM future_secrets WHERE used = 0 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    queue_id, secret, set_by = row
    conn.execute(
        "UPDATE future_secrets SET used = 1, used_date = ? WHERE id = ?", (date, queue_id)
    )
    return secret, set_by


def _purge_old_days(conn: sqlite3.Connection, date: str) -> None:
    """刪除 date 之前的每日紀錄(排行榜、猜測紀錄、練習題)。"""
    conn.execute("DELETE FROM guess_log WHERE date < ?", (date,))
    conn.execute("DELETE FROM user_day WHERE date < ?", (date,))
    conn.execute(
        "DELETE FROM practice_log WHERE game_id IN (SELECT id FROM practice_game WHERE date < ?)",
        (date,),
    )
    conn.execute("DELETE FROM practice_game WHERE date < ?", (date,))
    conn.execute("DELETE FROM daily_secret WHERE date < ?", (date,))


def get_today_secret(conn: sqlite3.Connection, date: str) -> tuple[str, str | None]:
    """回傳 (secret, set_by)。若當天題目不存在則建立(優先取排隊題目)。"""
    row = conn.execute(
        "SELECT secret, set_by FROM daily_secret WHERE date = ?", (date,)
    ).fetchone()
    if row:
        return row[0], row[1]

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT secret, set_by FROM daily_secret WHERE date = ?", (date,)
        ).fetchone()
        if row:
            conn.commit()
            return row[0], row[1]

        queued = _pop_queued_secret(conn, date)
        secret, set_by = queued if queued else (gen_random_secret(), None)

        conn.execute(
            "INSERT INTO daily_secret (date, secret, set_by, created_at) VALUES (?, ?, ?, ?)",
            (date, secret, set_by, now_iso()),
        )
        _purge_old_days(conn, date)
        conn.commit()
        return secret, set_by
    except Exception:
        conn.rollback()
        raise


def _get_user_day(conn: sqlite3.Connection, user_id: str, date: str) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT attempts, solved FROM user_day WHERE user_id = ? AND date = ?",
        (user_id, date),
    ).fetchone()
    if row is None:
        return 0, False
    return row[0], bool(row[1])


def _is_awaiting_secret(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT awaiting_secret FROM user_state WHERE user_id = ?", (user_id,)
    ).fetchone()
    return bool(row and row[0])


def _set_awaiting_secret(conn: sqlite3.Connection, user_id: str, value: bool) -> None:
    conn.execute(
        """
        INSERT INTO user_state (user_id, awaiting_secret) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET awaiting_secret = excluded.awaiting_secret
        """,
        (user_id, int(value)),
    )


def _record_guess(
    conn: sqlite3.Connection,
    user_id: str,
    date: str,
    attempt_no: int,
    guess: str,
    a: int,
    b: int,
    solved: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO guess_log (user_id, date, attempt_no, guess, a, b, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, date, attempt_no, guess, a, b, now_iso()),
    )
    conn.execute(
        """
        INSERT INTO user_day (user_id, date, attempts, solved, solved_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET
            attempts = attempts + 1,
            solved = excluded.solved,
            solved_at = excluded.solved_at
        """,
        (user_id, date, int(solved), now_iso() if solved else None),
    )


def _fetch_history(conn: sqlite3.Connection, user_id: str, date: str) -> list[tuple[str, int, int]]:
    rows = conn.execute(
        """
        SELECT guess, a, b FROM guess_log
        WHERE user_id = ? AND date = ?
        ORDER BY attempt_no
        """,
        (user_id, date),
    ).fetchall()
    return rows


def _format_history(rows: list[tuple[str, int, int]]) -> str:
    return "\n".join(f"{i + 1}. {g} → {a}A{b}B" for i, (g, a, b) in enumerate(rows))


def _get_admin(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT user_id FROM bot_admin LIMIT 1").fetchone()
    return row[0] if row else None


def _claim_admin(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute(
        "INSERT INTO bot_admin (user_id, claimed_at) VALUES (?, ?)", (user_id, now_iso())
    )


def _enqueue_future_secret(conn: sqlite3.Connection, user_id: str, secret: str) -> None:
    conn.execute(
        "INSERT INTO future_secrets (secret, set_by, created_at) VALUES (?, ?, ?)",
        (secret, user_id, now_iso()),
    )


def _admin_reset_today(conn: sqlite3.Connection, date: str, secret: str) -> None:
    conn.execute(
        """
        INSERT INTO daily_secret (date, secret, set_by, created_at) VALUES (?, ?, NULL, ?)
        ON CONFLICT(date) DO UPDATE SET
            secret = excluded.secret,
            set_by = NULL,
            created_at = excluded.created_at
        """,
        (date, secret, now_iso()),
    )
    conn.execute("DELETE FROM guess_log WHERE date = ?", (date,))
    conn.execute("DELETE FROM user_day WHERE date = ?", (date,))
    conn.execute(
        "DELETE FROM practice_log WHERE game_id IN (SELECT id FROM practice_game WHERE date = ?)",
        (date,),
    )
    conn.execute("DELETE FROM practice_game WHERE date = ?", (date,))


def _fetch_display_name(user_id: str) -> str | None:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return None
    try:
        with ApiClient(Configuration(access_token=token)) as api_client:
            return MessagingApi(api_client).get_profile(user_id).display_name
    except Exception:
        logger.exception("get_profile failed for %s", user_id)
        return None


def _save_display_name(conn: sqlite3.Connection, user_id: str) -> None:
    name = _fetch_display_name(user_id)
    if not name:
        return
    conn.execute(
        """
        INSERT INTO user_profile (user_id, display_name, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (user_id, name, now_iso()),
    )


def _format_leaderboard(conn: sqlite3.Connection, user_id: str, date: str) -> str:
    rows = conn.execute(
        """
        SELECT d.user_id, d.attempts, p.display_name
        FROM user_day d LEFT JOIN user_profile p ON p.user_id = d.user_id
        WHERE d.date = ? AND d.solved = 1
        ORDER BY d.attempts, d.solved_at
        """,
        (date,),
    ).fetchall()
    if not rows:
        return f"🏆 今日排行榜({date})\n還沒有人猜對,搶第一名吧!"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    ranked = []
    rank = 0
    prev_attempts = None
    for i, (uid, attempts, name) in enumerate(rows):
        if attempts != prev_attempts:
            rank = i + 1
            prev_attempts = attempts
        ranked.append((rank, uid, attempts, name or "神秘玩家"))

    def line(rank: int, uid: str, attempts: int, name: str) -> str:
        me = "(你)" if uid == user_id else ""
        return f"{medals.get(rank, f'{rank}.')} {name}{me} — {attempts} 次"

    lines = [f"🏆 今日排行榜({date})"]
    lines += [line(*r) for r in ranked[:LEADERBOARD_SIZE]]
    mine = next((r for r in ranked if r[1] == user_id), None)
    if mine and mine not in ranked[:LEADERBOARD_SIZE]:
        lines += ["⋯", line(*mine)]
    lines.append(f"共 {len(ranked)} 人猜對")
    return "\n".join(lines)


def _get_practice(conn: sqlite3.Connection, user_id: str, date: str) -> tuple[int, str, int, bool] | None:
    """回傳使用者今天最新一題練習題 (id, secret, attempts, solved)。"""
    row = conn.execute(
        """
        SELECT id, secret, attempts, solved FROM practice_game
        WHERE user_id = ? AND date = ?
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, date),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2], bool(row[3])


def _start_practice(conn: sqlite3.Connection, user_id: str, date: str) -> None:
    conn.execute(
        "INSERT INTO practice_game (user_id, date, secret, created_at) VALUES (?, ?, ?, ?)",
        (user_id, date, gen_random_secret(), now_iso()),
    )


def _fetch_practice_history(conn: sqlite3.Connection, game_id: int) -> list[tuple[str, int, int]]:
    return conn.execute(
        "SELECT guess, a, b FROM practice_log WHERE game_id = ? ORDER BY attempt_no",
        (game_id,),
    ).fetchall()


def _play_practice(conn: sqlite3.Connection, game_id: int, secret: str, attempts: int, guess: str) -> str:
    attempt_no = attempts + 1
    a, b = compute_feedback(secret, guess)
    won = a == 4
    conn.execute(
        """
        INSERT INTO practice_log (game_id, attempt_no, guess, a, b, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, attempt_no, guess, a, b, now_iso()),
    )
    conn.execute(
        "UPDATE practice_game SET attempts = ?, solved = ? WHERE id = ?",
        (attempt_no, int(won), game_id),
    )
    conn.commit()

    history_text = _format_history(_fetch_practice_history(conn, game_id))
    if won:
        return (
            f"🎉 練習題猜對!答案是 {guess},用了 {attempt_no} 次。\n\n"
            f"練習紀錄:\n{history_text}\n\n"
            "想再來一題就傳「next」(練習題不列入排行榜)"
        )
    return f"[練習] {a}A{b}B(第 {attempt_no} 次)\n\n練習紀錄:\n{history_text}"


def _broadcast(text: str) -> bool:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        return False
    try:
        with ApiClient(Configuration(access_token=token)) as api_client:
            MessagingApi(api_client).broadcast(
                BroadcastRequest(messages=[TextMessage(text=text)])
            )
        return True
    except Exception:
        logger.exception("broadcast failed")
        return False


def handle_message(user_id: str, text: str) -> str:
    text = text.strip()
    logger.info("message from %s: %r", user_id, text)
    conn = get_conn()
    try:
        if text in HELP_TRIGGERS:
            return HELP_TEXT

        if text in PROGRESS_TRIGGERS:
            date = today_str()
            attempts, solved = _get_user_day(conn, user_id, date)
            if attempts == 0:
                return "你今天還沒開始猜喔,傳一組 4 位數字試試看!"
            history = _format_history(_fetch_history(conn, user_id, date))
            status = "已經猜對 🎉" if solved else "還沒猜對"
            msg = f"今天狀態:{status}(共 {attempts} 次)\n\n{history}"
            practice = _get_practice(conn, user_id, date) if solved else None
            if practice and not practice[3] and practice[2] > 0:
                practice_history = _format_history(_fetch_practice_history(conn, practice[0]))
                msg += f"\n\n目前練習題(共 {practice[2]} 次):\n{practice_history}"
            return msg

        if text in LEADERBOARD_TRIGGERS:
            return _format_leaderboard(conn, user_id, today_str())

        if text == ADMIN_REVEAL_TRIGGER:
            admin_id = _get_admin(conn)
            if admin_id is None:
                _claim_admin(conn, user_id)
                conn.commit()
                admin_id = user_id
            if user_id == admin_id:
                date = today_str()
                secret, set_by = get_today_secret(conn, date)
                conn.commit()
                msg = f"🔐(僅管理員可見)今天({date})的答案是 {secret}"
                if set_by:
                    msg += "(這是玩家出的題目)"
                return msg
            # 非管理員:當成一般無法辨識的文字,不洩露這是個指令
            return HELP_TEXT

        reset_match = ADMIN_RESET_RE.match(text)
        if reset_match:
            admin_id = _get_admin(conn)
            if admin_id is None:
                _claim_admin(conn, user_id)
                conn.commit()
                admin_id = user_id
            if user_id == admin_id:
                new_secret = reset_match.group(1) or gen_random_secret()
                date = today_str()
                _admin_reset_today(conn, date, new_secret)
                conn.commit()
                broadcast_ok = _broadcast(RESET_ANNOUNCEMENT)
                note = "已經廣播通知大家了 📢" if broadcast_ok else "⚠️ 廣播通知失敗(檢查訊息額度或權限)"
                return (
                    f"✅ 今天({date})的題目已經重置成功,"
                    f"所有人今天的猜測紀錄與次數都已清空。{note}\n"
                    "(答案不會顯示在這裡,想看的話傳「成功了」)"
                )
            logger.warning("reset ignored: %s is not admin (%s)", user_id, admin_id)
            return HELP_TEXT

        if _is_awaiting_secret(conn, user_id):
            if text in SKIP_TRIGGERS:
                _set_awaiting_secret(conn, user_id, False)
                conn.commit()
                return "好的,已取消出題。想繼續玩可以傳「next」來一題練習題!"
            if GUESS_RE.match(text):
                if not _has_unique_digits(text):
                    return (
                        "出題的數字不能重複喔(4 個位置要是不同數字),"
                        "請重新傳一組,例如:1234\n"
                        "或傳「跳過」放棄這次機會。"
                    )
                _enqueue_future_secret(conn, user_id, text)
                _set_awaiting_secret(conn, user_id, False)
                conn.commit()
                return (
                    f"收到!你出的題目「{text}」已經排入未來的每日題目,"
                    "敬請期待某天大家挑戰它 🎯\n"
                    "(想繼續玩可以傳「next」來一題練習題)"
                )
            return (
                "你還有一個「出題資格」沒用喔!\n"
                "請傳一組 4 位不重複的數字(0-9)當作未來的每日題目,\n"
                "或傳「跳過」放棄這次機會。\n"
                "(出完題或跳過後,就可以傳「next」玩練習題)"
            )

        if text.lower() in NEXT_TRIGGERS:
            date = today_str()
            _, solved = _get_user_day(conn, user_id, date)
            if not solved:
                return "要先猜對今天的每日題目,才能玩練習題喔!傳一組 4 位數字試試看。"
            prev = _get_practice(conn, user_id, date)
            _start_practice(conn, user_id, date)
            conn.commit()
            msg = ""
            if prev and not prev[3]:
                msg = f"上一題練習題的答案是 {prev[1]}。\n\n"
            return msg + (
                "🎲 新的練習題來了!一樣是 4 位不重複的數字,傳 4 位數字開始猜。\n"
                "(練習題不列入排行榜,明天會換新的每日題目)"
            )

        if not GUESS_RE.match(text):
            return HELP_TEXT

        date = today_str()
        secret, set_by = get_today_secret(conn, date)
        attempts, solved = _get_user_day(conn, user_id, date)

        if solved:
            practice = _get_practice(conn, user_id, date)
            if practice and not practice[3]:
                game_id, practice_secret, practice_attempts, _ = practice
                return _play_practice(conn, game_id, practice_secret, practice_attempts, text)
            return (
                f"你今天已經猜對囉!答案是 {secret}。\n"
                "想繼續玩可以傳「next」來一題練習題(不列入排行榜)😄"
            )

        attempt_no = attempts + 1
        a, b = compute_feedback(secret, text)
        won = a == 4
        _record_guess(conn, user_id, date, attempt_no, text, a, b, won)
        conn.commit()

        history_text = _format_history(_fetch_history(conn, user_id, date))

        if won:
            _save_display_name(conn, user_id)
            conn.commit()
            msg = f"🎉 恭喜猜對!答案就是 {text},你總共用了 {attempt_no} 次。"
            if set_by:
                msg += "\n(今天的題目是其他玩家出的喔!)"
            msg += f"\n\n本日紀錄:\n{history_text}"
            msg += f"\n\n{_format_leaderboard(conn, user_id, date)}"
            if attempt_no <= WIN_BONUS_ATTEMPTS:
                _set_awaiting_secret(conn, user_id, True)
                conn.commit()
                msg += (
                    f"\n\n🌟 你在 {WIN_BONUS_ATTEMPTS} 次以內解出,獲得「出題資格」!\n"
                    "下一則訊息請傳一組 4 位不重複的數字(0-9),"
                    "將成為未來某一天的每日題目。想放棄可傳「跳過」。\n"
                    "出完題後可以傳「next」玩練習題。"
                )
            else:
                msg += "\n\n想繼續玩?傳「next」來一題練習題(不列入排行榜)"
            return msg

        return f"{a}A{b}B(第 {attempt_no} 次)\n\n本日紀錄:\n{history_text}"
    finally:
        conn.close()
