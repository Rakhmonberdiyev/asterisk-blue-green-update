import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT")
)
cur = conn.cursor()

def get_or_create_user(phone_number: str, lan: str = "uz") -> int:
    """Phone raqam bo'yicha user topadi yoki yangi yaratadi."""
    try:
        cur.execute(
            'SELECT id FROM users WHERE phone_number = %s;',
            (phone_number,)
        )
        row = cur.fetchone()
        if row:
            return row[0]
        
        # Yangi user - id sifatida phone hash yoki sequence ishlatamiz
        cur.execute(
            """
            INSERT INTO users (id, name, phone_number, lan)
            VALUES (nextval('users_id_seq'), %s, %s, %s)
            RETURNING id;
            """,
            (phone_number, phone_number, lan)
        )
        conn.commit()
        return cur.fetchone()[0]
    except Exception as e:
        conn.rollback()
        print(f"Error in get_or_create_user: {e}")
        return None

def create_session(user_id: int) -> int:
    try:
        cur.execute(
            """
            INSERT INTO sessions (rating, rated, notify, user_id)
            VALUES (0, FALSE, FALSE, %s)
            RETURNING id;
            """,
            (user_id,)
        )
        conn.commit()
        return cur.fetchone()[0]
    except Exception as e:
        conn.rollback()
        print(f"Error in create_session: {e}")
        raise e

def update_session_recordings(session_id: int, 
                               client_path: str = None, 
                               ai_path: str = None, 
                               combined_path: str = None):
    try:
        cur.execute(
            """
            UPDATE sessions SET
                call_client_recording_path = COALESCE(%s, call_client_recording_path),
                call_ai_recording_path     = COALESCE(%s, call_ai_recording_path),
                call_recording_path        = COALESCE(%s, call_recording_path)
            WHERE id = %s;
            """,
            (client_path, ai_path, combined_path, session_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error in update_session_recordings: {e}")

def create_conversation(role: str, message: str, speech_path: str, session_id: int) -> int:
    try:
        cur.execute(
            """
            INSERT INTO conversations (role, message, speech_path, session_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
            """,
            (role, message, speech_path, session_id)
        )
        conn.commit()
        return cur.fetchone()[0]
    except Exception as e:
        conn.rollback()
        print(f"Error in create_conversation: {e}")
        return None

def rate_session(session_id: int, rating: int, rated=True, notify=True):
    try:
        cur.execute(
            """
            UPDATE sessions
            SET rating = %s, rated = %s, notify = %s
            WHERE id = %s;
            """,
            (rating, rated, notify, session_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error in rate_session: {e}")




def get_last_2_path_of_user_speech(session_id: int):
    try:
        cur.execute(
            "SELECT speech_path FROM conversations where role = 'user' and session_id = %s order by time desc limit 2;",
            (session_id,)
        )
        rows = cur.fetchall()
        return rows
        
    except Exception as e:
        conn.rollback()
        print(f"Error in get_all_path: {e}")
        return None



def get_all_path():
    try:
        cur.execute(
            'SELECT call_recording_path FROM sessions'
        )
        rows = cur.fetchall()
        return rows
        
    except Exception as e:
        conn.rollback()
        print(f"Error in get_all_path: {e}")
        return None