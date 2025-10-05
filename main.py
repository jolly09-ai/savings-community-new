# Savings Community App - Backend
# This app tracks savings goals, sacrifices, and streaks

import os
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from jose import jwt, JWTError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import sqlite3
from contextlib import contextmanager
from functools import lru_cache

# ============ CONFIGURATION ============
class Settings(BaseSettings):
    database_url: str = "sqlite:///./savings.db"
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str = "http://127.0.0.1:8002/auth/google/callback"
    jwt_secret: str = "change_this_secret"
    jwt_alg: str = "HS256"

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings():
    return Settings()

settings = get_settings()

# ============ DATABASE SETUP ============
DB_PATH = "savings.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        avatar_url TEXT,
        google_sub TEXT UNIQUE NOT NULL,
        total_saved REAL DEFAULT 0,
        current_streak INTEGER DEFAULT 0,
        last_save_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Goals table
    c.execute('''CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        target_amount REAL NOT NULL,
        current_amount REAL DEFAULT 0,
        category TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    # Sacrifices table (activities users skip to save money)
    c.execute('''CREATE TABLE IF NOT EXISTS sacrifices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        amount REAL NOT NULL,
        days_count INTEGER DEFAULT 1,
        last_done_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    # Feed events table
    c.execute('''CREATE TABLE IF NOT EXISTS feed_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    
    conn.commit()
    
    # Add dummy users if database is empty
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        create_dummy_data(c, conn)
    
    conn.close()

def create_dummy_data(c, conn):
    """Create dummy users and data for demo"""
    import json
    
    # Dummy users
    users = [
        ("dummy1@example.com", "Richa Gupta", "https://i.pravatar.cc/150?img=1", "dummy-1", 421.50, 5),
        ("dummy2@example.com", "James Chen", "https://i.pravatar.cc/150?img=12", "dummy-2", 285.00, 3),
        ("dummy3@example.com", "Sarah Johnson", "https://i.pravatar.cc/150?img=5", "dummy-3", 567.25, 7),
    ]
    
    for email, name, avatar, sub, saved, streak in users:
        c.execute(
            "INSERT INTO users (email, name, avatar_url, google_sub, total_saved, current_streak, last_save_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, name, avatar, sub, saved, streak, datetime.now().isoformat())
        )
    
    conn.commit()
    
    # Get user IDs
    c.execute("SELECT id FROM users WHERE email LIKE 'dummy%' ORDER BY id")
    user_ids = [row[0] for row in c.fetchall()]
    
    # Goals for users
    goals = [
        (user_ids[0], "Concert Tickets", 180.0, 45.0, "Entertainment"),
        (user_ids[0], "Weekend Trip", 400.0, 120.0, "Travel"),
        (user_ids[1], "New Laptop", 800.0, 285.0, "Technology"),
        (user_ids[2], "Emergency Fund", 1000.0, 567.25, "Savings"),
    ]
    
    for user_id, title, target, current, category in goals:
        c.execute(
            "INSERT INTO goals (user_id, title, target_amount, current_amount, category) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, target, current, category)
        )
    
    # Sacrifices
    sacrifices = [
        (user_ids[0], "Skipped Latte", 4.50, 4, datetime.now().isoformat()),
        (user_ids[0], "Packed Lunch", 9.50, 5, datetime.now().isoformat()),
        (user_ids[1], "No Takeout", 15.00, 3, datetime.now().isoformat()),
        (user_ids[2], "Walked Instead", 8.00, 7, datetime.now().isoformat()),
    ]
    
    for user_id, title, amount, days, date in sacrifices:
        c.execute(
            "INSERT INTO sacrifices (user_id, title, amount, days_count, last_done_date) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, amount, days, date)
        )
    
    # Feed events
    c.execute("SELECT id, user_id, title FROM goals")
    for goal_id, user_id, title in c.fetchall():
        event_data = json.dumps({"goal_id": goal_id, "title": title})
        c.execute(
            "INSERT INTO feed_events (user_id, event_type, event_data) VALUES (?, ?, ?)",
            (user_id, "goal_created", event_data)
        )
    
    c.execute("SELECT id, user_id, title, days_count FROM sacrifices")
    for sac_id, user_id, title, days in c.fetchall():
        event_data = json.dumps({"sacrifice_id": sac_id, "title": title, "days": days})
        c.execute(
            "INSERT INTO feed_events (user_id, event_type, event_data) VALUES (?, ?, ?)",
            (user_id, "sacrifice_logged", event_data)
        )
    
    conn.commit()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ============ APP INITIALIZATION ============
app = FastAPI(title="Savings Community", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    init_db()

# ============ AUTH HELPERS ============
google_request = google_requests.Request()

def create_jwt_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)

def get_current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")
    
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        user_id = int(payload["sub"])
        return user_id
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

# ============ PYDANTIC MODELS ============
class GoalCreate(BaseModel):
    title: str
    target_amount: float
    category: str = "General"

class SacrificeCreate(BaseModel):
    title: str
    amount: float

# ============ ROOT & AUTH ROUTES ============
@app.get("/")
def root():
    return FileResponse("index.html")

@app.get("/auth/google/login")
def google_login():
    scope = "openid email profile"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={settings.google_client_id}"
        f"&redirect_uri={settings.google_redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
    )
    return RedirectResponse(url)

@app.get("/auth/google/callback")
async def google_callback(code: str):
    import httpx
    
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_redirect_uri
            }
        )
        
        if token_resp.status_code != 200:
            raise HTTPException(400, "Failed to get Google tokens")
        
        tokens = token_resp.json()
        
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        
        if userinfo_resp.status_code != 200:
            raise HTTPException(400, "Failed to get user info")
        
        info = userinfo_resp.json()
    
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE google_sub = ?", (info["sub"],))
        user = c.fetchone()
        
        if not user:
            c.execute(
                "INSERT INTO users (email, name, avatar_url, google_sub) VALUES (?, ?, ?, ?)",
                (info.get("email"), info.get("name"), info.get("picture"), info["sub"])
            )
            conn.commit()
            user_id = c.lastrowid
        else:
            user_id = user["id"]
    
    access_token = create_jwt_token(user_id)
    
    return RedirectResponse(f"/?token={access_token}")

# ============ USER ROUTES ============
@app.get("/api/me")
def get_me(user_id: int = Depends(get_current_user)):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "User not found")
        return dict(user)

@app.get("/api/dashboard")
def get_dashboard(user_id: int = Depends(get_current_user)):
    with get_db() as conn:
        c = conn.cursor()
        
        # User stats
        c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = dict(c.fetchone())
        
        # Active goals
        c.execute("SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        goals = [dict(row) for row in c.fetchall()]
        
        # Recent sacrifices
        c.execute("SELECT * FROM sacrifices WHERE user_id = ? ORDER BY created_at DESC LIMIT 5", (user_id,))
        sacrifices = [dict(row) for row in c.fetchall()]
        
        return {
            "user": user,
            "goals": goals,
            "sacrifices": sacrifices
        }

@app.get("/api/feed")
def get_feed():
    import json
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT f.*, u.name, u.avatar_url 
            FROM feed_events f
            JOIN users u ON f.user_id = u.id
            ORDER BY f.created_at DESC
            LIMIT 20
        """)
        
        events = []
        for row in c.fetchall():
            event = dict(row)
            event['event_data'] = json.loads(event['event_data'])
            events.append(event)
        
        return events

@app.get("/api/leaderboard")
def get_leaderboard():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, name, avatar_url, total_saved, current_streak
            FROM users
            ORDER BY total_saved DESC
            LIMIT 10
        """)
        return [dict(row) for row in c.fetchall()]

@app.post("/api/goals")
def create_goal(goal: GoalCreate, user_id: int = Depends(get_current_user)):
    import json
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO goals (user_id, title, target_amount, category) VALUES (?, ?, ?, ?)",
            (user_id, goal.title, goal.target_amount, goal.category)
        )
        goal_id = c.lastrowid
        
        # Add to feed
        event_data = json.dumps({"goal_id": goal_id, "title": goal.title})
        c.execute(
            "INSERT INTO feed_events (user_id, event_type, event_data) VALUES (?, ?, ?)",
            (user_id, "goal_created", event_data)
        )
        
        conn.commit()
        return {"id": goal_id, "title": goal.title}

@app.post("/api/sacrifices")
def log_sacrifice(sacrifice: SacrificeCreate, user_id: int = Depends(get_current_user)):
    import json
    with get_db() as conn:
        c = conn.cursor()
        
        # Check if sacrifice exists
        c.execute("SELECT * FROM sacrifices WHERE user_id = ? AND title = ?", (user_id, sacrifice.title))
        existing = c.fetchone()
        
        if existing:
            # Update days count
            new_days = existing["days_count"] + 1
            c.execute(
                "UPDATE sacrifices SET days_count = ?, last_done_date = ? WHERE id = ?",
                (new_days, datetime.now().isoformat(), existing["id"])
            )
            sacrifice_id = existing["id"]
        else:
            # Create new
            c.execute(
                "INSERT INTO sacrifices (user_id, title, amount, last_done_date) VALUES (?, ?, ?, ?)",
                (user_id, sacrifice.title, sacrifice.amount, datetime.now().isoformat())
            )
            sacrifice_id = c.lastrowid
            new_days = 1
        
        # Update user total saved and streak
        c.execute(
            "UPDATE users SET total_saved = total_saved + ?, current_streak = current_streak + 1 WHERE id = ?",
            (sacrifice.amount, user_id)
        )
        
        # Add to feed
        event_data = json.dumps({"sacrifice_id": sacrifice_id, "title": sacrifice.title, "days": new_days})
        c.execute(
            "INSERT INTO feed_events (user_id, event_type, event_data) VALUES (?, ?, ?)",
            (user_id, "sacrifice_logged", event_data)
        )
        
        conn.commit()
        return {"message": "Sacrifice logged", "days": new_days}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)