"""
Хранение расписания, статистики, комментариев и групп в PostgreSQL (Render).
При отсутствии DATABASE_URL менеджеры в app.py используют файлы.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

logger = logging.getLogger("travel-post-generator")

_conn = None

def _get_connection():
    global _conn
    if _conn is not None:
        try:
            _conn.cursor().execute("SELECT 1")
            return _conn
        except Exception:
            _conn = None
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        # Render PostgreSQL требует sslmode=require
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(
            url,
            connect_timeout=10,
            options="-c statement_timeout=10000",
        )
        conn.autocommit = True
        _conn = conn
        logger.info("Подключение к PostgreSQL установлено")
        return conn
    except Exception as e:
        logger.exception("Ошибка подключения к PostgreSQL: %s", e)
        return None

def init_db() -> bool:
    """Создаёт таблицы, если их нет. Возвращает True при успехе."""
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schedule (
                    id INT PRIMARY KEY DEFAULT 1,
                    next_post_time VARCHAR(10),
                    frequency_hours INT NOT NULL DEFAULT 24,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    next_run_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("""
                INSERT INTO schedule (id, frequency_hours, enabled)
                VALUES (1, 24, TRUE)
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stats_posts (
                    id SERIAL PRIMARY KEY,
                    post_id VARCHAR(64),
                    text_id VARCHAR(64),
                    photo_id VARCHAR(64),
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    views INT NOT NULL DEFAULT 0,
                    comments_count INT NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id SERIAL PRIMARY KEY,
                    chat_id VARCHAR(64) NOT NULL,
                    message_id VARCHAR(64),
                    text TEXT NOT NULL,
                    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    id SERIAL PRIMARY KEY,
                    group_id VARCHAR(64) UNIQUE NOT NULL,
                    title VARCHAR(256) NOT NULL DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
        logger.info("Таблицы БД инициализированы")
        return True
    except Exception as e:
        logger.exception("Ошибка инициализации БД: %s", e)
        return False

# --- Schedule ---

def db_schedule_load() -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT next_post_time, frequency_hours, enabled, next_run_at
                FROM schedule WHERE id = 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            next_post_time, frequency_hours, enabled, next_run_at = row
            return {
                "next_post_time": next_post_time,
                "frequency_hours": frequency_hours or 24,
                "enabled": bool(enabled),
                "next_run_at": next_run_at.isoformat() if next_run_at else None,
            }
    except Exception as e:
        logger.exception("db_schedule_load: %s", e)
        return None

def db_schedule_save(schedule: Dict[str, Any]) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        next_run = schedule.get("next_run_at")
        if isinstance(next_run, datetime):
            next_run = next_run.isoformat()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE schedule SET
                    next_post_time = %s, frequency_hours = %s, enabled = %s,
                    next_run_at = %s, updated_at = NOW()
                WHERE id = 1
            """, (
                schedule.get("next_post_time"),
                schedule.get("frequency_hours", 24),
                schedule.get("enabled", True),
                next_run,
            ))
        return True
    except Exception as e:
        logger.exception("db_schedule_save: %s", e)
        return False

# --- Stats (posts) ---

def db_stats_add_post(post_id: str, text_id: Optional[str], photo_id: Optional[str]) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO stats_posts (post_id, text_id, photo_id, views, comments_count)
                VALUES (%s, %s, %s, 0, 0)
            """, (post_id, text_id, photo_id))
            cur.execute("""
                DELETE FROM stats_posts WHERE id NOT IN (
                    SELECT id FROM stats_posts ORDER BY id DESC LIMIT 100
                )
            """)
        return True
    except Exception as e:
        logger.exception("db_stats_add_post: %s", e)
        return False

def db_stats_update_post(post_id: str, views: Optional[int], comments: Optional[int]) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            if views is not None and comments is not None:
                cur.execute(
                    "UPDATE stats_posts SET views = %s, comments_count = %s WHERE post_id = %s OR text_id = %s OR photo_id = %s",
                    (views, comments, post_id, post_id, post_id)
                )
            elif views is not None:
                cur.execute(
                    "UPDATE stats_posts SET views = %s WHERE post_id = %s OR text_id = %s OR photo_id = %s",
                    (views, post_id, post_id, post_id)
                )
            elif comments is not None:
                cur.execute(
                    "UPDATE stats_posts SET comments_count = %s WHERE post_id = %s OR text_id = %s OR photo_id = %s",
                    (comments, post_id, post_id, post_id)
                )
        return True
    except Exception as e:
        logger.exception("db_stats_update_post: %s", e)
        return False

def db_stats_get_recent(days: int) -> Dict[str, Any]:
    conn = _get_connection()
    default = {
        "period_days": days, "total_posts": 0, "total_views": 0, "total_comments": 0,
        "avg_views": 0.0, "avg_comments": 0.0, "posts": [],
    }
    if not conn:
        return default
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT post_id, text_id, photo_id, ts, views, comments_count
                FROM stats_posts
                WHERE ts >= NOW() - INTERVAL '1 day' * %s
                ORDER BY ts DESC
            """, (days,))
            rows = cur.fetchall()
        posts = []
        total_views = total_comments = 0
        for row in rows:
            post_id, text_id, photo_id, ts, views, comments_count = row
            total_views += views or 0
            total_comments += comments_count or 0
            posts.append({
                "post_id": post_id,
                "text_id": text_id,
                "photo_id": photo_id,
                "timestamp": ts.isoformat() if ts else None,
                "views": views or 0,
                "comments": comments_count or 0,
            })
        n = len(posts)
        return {
            "period_days": days,
            "total_posts": n,
            "total_views": total_views,
            "total_comments": total_comments,
            "avg_views": round(total_views / n, 1) if n else 0,
            "avg_comments": round(total_comments / n, 1) if n else 0,
            "posts": posts[-10:],
        }
    except Exception as e:
        logger.exception("db_stats_get_recent: %s", e)
        return default

# --- Comments ---

def db_comments_add(chat_id: str, message_id: Optional[str], text: str, timestamp: Optional[str] = None) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        ts = datetime.fromisoformat(timestamp) if timestamp else datetime.now()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO comments (chat_id, message_id, text, ts) VALUES (%s, %s, %s, %s)",
                (str(chat_id), str(message_id) if message_id else None, text, ts)
            )
            cur.execute("""
                DELETE FROM comments WHERE id NOT IN (
                    SELECT id FROM comments ORDER BY id DESC LIMIT 200
                )
            """)
        return True
    except Exception as e:
        logger.exception("db_comments_add: %s", e)
        return False

def db_comments_get_latest_any() -> Optional[str]:
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM comments ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.exception("db_comments_get_latest_any: %s", e)
        return None

def db_comments_get_latest_for_chat(chat_id: str) -> Optional[str]:
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM comments WHERE chat_id = %s ORDER BY id DESC LIMIT 1",
                (str(chat_id),)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.exception("db_comments_get_latest_for_chat: %s", e)
        return None

# --- Groups ---

def db_groups_load() -> tuple:
    """Возвращает (list of {group_id, title}, active_group_id)."""
    conn = _get_connection()
    if not conn:
        return [], None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id, title, is_active FROM groups ORDER BY id")
            rows = cur.fetchall()
        groups = [{"group_id": r[0], "title": r[1] or ""} for r in rows]
        active = None
        for r in rows:
            if r[2]:
                active = r[0]
                break
        if not active and groups:
            active = groups[0]["group_id"]
        return groups, active
    except Exception as e:
        logger.exception("db_groups_load: %s", e)
        return [], None

def db_groups_save(groups: List[Dict[str, Any]], active_group_id: Optional[str]) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM groups")
            for g in groups:
                gid = g.get("group_id") or ""
                title = g.get("title") or ""
                is_active = str(gid) == str(active_group_id)
                cur.execute(
                    "INSERT INTO groups (group_id, title, is_active) VALUES (%s, %s, %s)",
                    (gid, title, is_active)
                )
        return True
    except Exception as e:
        logger.exception("db_groups_save: %s", e)
        return False

def db_groups_add(group_id: str, title: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO groups (group_id, title, is_active) VALUES (%s, %s, FALSE) ON CONFLICT (group_id) DO UPDATE SET title = EXCLUDED.title",
                (str(group_id), title or str(group_id)),
            )
        return True
    except Exception as e:
        logger.exception("db_groups_add: %s", e)
        return False

def db_groups_set_active(group_id: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE groups SET is_active = FALSE")
            cur.execute("UPDATE groups SET is_active = TRUE WHERE group_id = %s", (str(group_id),))
            if cur.rowcount == 0:
                cur.execute("INSERT INTO groups (group_id, title, is_active) VALUES (%s, %s, TRUE)", (str(group_id), f"Группа {group_id}"))
        return True
    except Exception as e:
        logger.exception("db_groups_set_active: %s", e)
        return False
