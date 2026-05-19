import os
from dotenv import load_dotenv
import pymysql
from clickhouse_driver import Client

load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', '3306')),
    'database': os.getenv('DB_NAME', 'stocker'),
    'user': os.getenv('DB_USER', ''),
    'password': os.getenv('DB_PASSWORD', ''),
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

CH_CONFIG = {
    'host': os.getenv('CLICKHOUSE_HOST', '172.17.0.4'),
    'port': int(os.getenv('CLICKHOUSE_PORT', '9000')),
    'user': os.getenv('CLICKHOUSE_USER', 'jinseoki'),
    'password': os.getenv('CLICKHOUSE_PASSWORD', 'C!@jskk4343'),
    'database': 'stocker',
}

def get_db_connection():
    return pymysql.connect(**DB_CONFIG)

def get_clickhouse_client():
    return Client(**CH_CONFIG)