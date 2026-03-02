"""
Simple Oracle DB connection test using parameters from .env.
Uses ORACLE_NL2SQL_* variables; config_dir and wallet_location both come from ORACLE_NL2SQL_WALLET_PATH.
"""
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the same directory as this script
load_dotenv(Path(__file__).resolve().parent / ".env")

import os
import oracledb

def main():
    wallet_path = (os.environ.get("ORACLE_NL2SQL_WALLET_PATH") or "").strip()
    user = (os.environ.get("ORACLE_NL2SQL_USER") or "").strip()
    password = (os.environ.get("ORACLE_NL2SQL_PASSWORD") or "").strip()
    dsn = (os.environ.get("ORACLE_NL2SQL_DSN") or "").strip()
    wallet_password = (os.environ.get("ORACLE_NL2SQL_WALLET_PASSWORD") or "").strip() or None

    if not all([wallet_path, user, password, dsn]):
        print("Missing required env vars: ORACLE_NL2SQL_WALLET_PATH, ORACLE_NL2SQL_USER, ORACLE_NL2SQL_PASSWORD, ORACLE_NL2SQL_DSN")
        return 1

    try:
        connection = oracledb.connect(
            config_dir='/Users/ashwins/Desktop/T2D/Wallet_TECPDATP01',
            user='NL2SQL',
            password='a#rt9Ilkm12rai',
            dsn='tecpdatp01_high',
            wallet_location='/Users/ashwins/Desktop/T2D/Wallet_TECPDATP01',
            wallet_password='uMljPE@qwuInbE11',
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM DUAL")
            row = cursor.fetchone()
            print("Connected successfully. SELECT 1 FROM DUAL =>", row)
        connection.close()
        return 0
    except Exception as e:
        print("Connection failed:", e)
        return 1

if __name__ == "__main__":
    exit(main())
