import os
import psycopg2

from dotenv import load_dotenv

load_dotenv()



conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT"))
)
cur = conn.cursor()


create_tables_sql = """

CREATE TABLE IF NOT EXISTS "users" (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    lan VARCHAR(20) NOT NULL DEFAULT 'uz',
    created TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    role VARCHAR(20) NOT NULL DEFAULT 'admin',   -- 'superadmin' | 'admin'
    is_active BOOLEAN DEFAULT TRUE,
    created TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES "users"(id),
    rating INT NOT NULL DEFAULT 0,
    rated BOOLEAN DEFAULT FALSE,
    notify BOOLEAN DEFAULT FALSE,
    call_client_recording_path VARCHAR(255),
    call_ai_recording_path VARCHAR(255),
    call_recording_path VARCHAR(255),
    created TIMESTAMP DEFAULT NOW(),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    message TEXT,
    speech_path VARCHAR(255),
    role VARCHAR(20) NOT NULL,
    time TIMESTAMP DEFAULT NOW(),
    session_id INT REFERENCES sessions(id)
);

"""
cur.execute(create_tables_sql)
conn.commit()
print("Tables created successfully!")

cur.close()
conn.close()