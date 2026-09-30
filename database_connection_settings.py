import os

import pymysql

DATABASE_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DATABASE_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DATABASE_USER = os.getenv("MYSQL_USER", "root")
DATABASE_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DATABASE_NAME = os.getenv("MYSQL_DATABASE", "parcel_delivery")


def open_database_connection(select_database=True, autocommit=False):
    return pymysql.connect(
        host=DATABASE_HOST,
        port=DATABASE_PORT,
        user=DATABASE_USER,
        password=DATABASE_PASSWORD,
        database=DATABASE_NAME if select_database else None,
        charset="utf8mb4",
        autocommit=autocommit,
    )
