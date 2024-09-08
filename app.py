from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import mysql.connector
import threading
import time
import sys

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Database configuration
DB_CONFIG = {
    "host": "stockchecker-stockchecker.g.aivencloud.com",
    "port": 26155,
    "database": "defaultdb",
    "user": "avnadmin",
    "password": ""
}

# Global lock for database operations
db_lock = threading.Lock()

def create_database_connection():
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        return connection
    except mysql.connector.Error as err:
        print(f"Error connecting to database: {err}")
        return None

def create_table_if_not_exists(connection):
    if connection is None:
        print("Database connection is not available.")
        return
    create_table_query = """
    CREATE TABLE IF NOT EXISTS stock_tracking_results (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ticker_id VARCHAR(10),
        ticker_name VARCHAR(255),
        price DECIMAL(10, 2),
        confidence_level DECIMAL(5, 2),
        initial_run_date DATETIME,
        batch_id INT
    )
    """
    try:
        with db_lock:
            with connection.cursor() as cursor:
                cursor.execute(create_table_query)
                connection.commit()
    except mysql.connector.Error as err:
        print(f"Error creating table: {err}")

def get_next_batch_id(connection, ticker):
    if connection is None:
        print("Database connection is not available.")
        return None
    query = "SELECT MAX(batch_id) FROM stock_tracking_results WHERE ticker_id = %s"
    try:
        with db_lock:
            with connection.cursor() as cursor:
                cursor.execute(query, (ticker,))
                result = cursor.fetchone()
                if result[0] is None:
                    return 1
                else:
                    return result[0] + 1
    except mysql.connector.Error as err:
        print(f"Error getting next batch ID: {err}")
        return None

def insert_tracking_result(connection, ticker_id, ticker_name, price, confidence_level, initial_run_date, batch_id):
    if connection is None:
        print("Database connection is not available.")
        return
    insert_query = """
    INSERT INTO stock_tracking_results 
    (ticker_id, ticker_name, price, confidence_level, initial_run_date, batch_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    try:
        confidence_decimal = float(confidence_level.strip('%')) / 100
        with db_lock:
            with connection.cursor() as cursor:
                cursor.execute(insert_query, (ticker_id, ticker_name, price, confidence_decimal, initial_run_date, batch_id))
                connection.commit()
    except mysql.connector.Error as err:
        print(f"Error inserting data: {err}")

def parse_time(time_str):
    if time_str.endswith('d'):
        return timedelta(days=int(time_str[:-1]))
    elif time_str.endswith('m'):
        return timedelta(days=int(time_str[:-1]) * 30)  # Approximate a month as 30 days
    elif time_str.endswith('min'):
        return timedelta(minutes=int(time_str[:-3]))
    elif time_str.endswith('sec'):
        return timedelta(seconds=int(time_str[:-3]))
    elif time_str.endswith('h'):
        return timedelta(hours=int(time_str[:-1]))
    else:
        raise ValueError("Invalid time format. Use 'd' for days, 'm' for months, 'min' for minutes, 'sec' for seconds, 'h' for hours.")

def get_current_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        current_price = stock.info['currentPrice']
        return current_price
    except:
        print(f"Unable to fetch price for {ticker}. Please check if the ticker is valid.")
        return None

def format_time(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def countdown_timer(seconds, ticker):
    start_time = time.time()
    while time.time() - start_time < seconds:
        remaining = int(seconds - (time.time() - start_time))
        timer_str = f"Time remaining: {format_time(remaining)}"
        socketio.emit('timer_update', {'ticker': ticker, 'timer': timer_str})
        time.sleep(0.5)  # Update every half second


def track_stock(ticker, initial_price, confidence, tracking_time, interval):
    print(f"Tracking stock: {ticker}")
    connection = create_database_connection()
    if connection is None:
        print(f"Database connection failed for {ticker}")
        socketio.emit('error', {'ticker': ticker, 'message': 'Database connection failed'})
        return

    try:
        tracking_duration = parse_time(tracking_time)
        interval_duration = parse_time(interval)

        start_time = datetime.now()
        end_time = start_time + tracking_duration

        batch_id = get_next_batch_id(connection, ticker)
        if batch_id is None:
            print(f"Unable to get batch ID for {ticker}. Tracking cancelled.")
            socketio.emit('error', {'ticker': ticker, 'message': f'Unable to get batch ID for {ticker}. Tracking cancelled.'})
            return

        print(f"Starting Batch ID for {ticker}: {batch_id}")
        socketio.emit('update', {'ticker': ticker, 'message': f"Starting Batch ID: {batch_id}"})

        update_count = 0
        expected_updates = int(tracking_duration.total_seconds() // interval_duration.total_seconds())

        for i in range(expected_updates):
            current_time = start_time + (i * interval_duration)
            current_price = get_current_price(ticker)
            
            if current_price is not None:
                insert_tracking_result(connection, ticker, ticker, current_price, confidence, current_time, batch_id)
                progress = min((i + 1) / expected_updates, 1.0)
                update_count += 1
                print(f"Update {update_count} for {ticker}: Price=${current_price:.2f}, Progress={progress:.2%}")
                socketio.emit('update', {
                    'ticker': ticker,
                    'price': f"${current_price:.2f}",
                    'progress': progress,
                    'update_count': update_count
                })
            else:
                print(f"Unable to fetch price for {ticker}")
                socketio.emit('error', {'ticker': ticker, 'message': f"Unable to fetch price for {ticker}"})

            # Start countdown timer for next update
            if i < expected_updates - 1:  # Don't start timer after last update
                socketio.emit('tracking_status', {'ticker': ticker, 'status': 'countdown', 'interval': interval_duration.total_seconds()})
                countdown_timer(interval_duration.total_seconds(), ticker)
            
            # Sleep until the next update time
            next_update_time = start_time + ((i + 1) * interval_duration)
            sleep_duration = (next_update_time - datetime.now()).total_seconds()
            if sleep_duration > 0:
                socketio.emit('tracking_status', {'ticker': ticker, 'status': 'tracking'})
                time.sleep(sleep_duration)

        print(f"Tracking completed for {ticker}. Total updates: {update_count}")
        socketio.emit('complete', {'ticker': ticker, 'message': f"Tracking completed for {ticker}! Total updates: {update_count}"})

    except ValueError as e:
        print(f"Error tracking {ticker}: {str(e)}")
        socketio.emit('error', {'ticker': ticker, 'message': f"Error tracking {ticker}: {str(e)}"})
    finally:
        if connection:
            connection.close()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('start_tracking')
def handle_start_tracking(data):
    print(f"Received start_tracking event: {data}")
    ticker = data['ticker']
    initial_price = data['initial_price']
    confidence = data['confidence']
    tracking_time = data['tracking_time']
    interval = data['interval']

    print(f"Starting tracking for {ticker}")
    thread = threading.Thread(target=track_stock, args=(ticker, initial_price, confidence, tracking_time, interval))
    thread.start()
    print(f"Tracking thread started for {ticker}")

if __name__ == '__main__':
    connection = create_database_connection()
    if connection:
        create_table_if_not_exists(connection)
        connection.close()
    print("Starting Flask-SocketIO app")
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
