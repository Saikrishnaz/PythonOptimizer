import os
import sys
from flask import Flask, render_template, request, jsonify
from data_engine import DataEngine
from indicators import IndicatorRegistry

app = Flask(__name__, template_folder="templates", static_folder="static")

PARQUET_FILE = r"c:\Users\ADMIN\Desktop\Charts\XAUUSD.._202601020100_202608101443.parquet"

# Initialize Data Engine
engine = DataEngine(PARQUET_FILE)

@app.before_request
def initialize_engine_once():
    if engine.base_1m_df is None:
        print("[Server] Pre-loading Parquet data engine...")
        engine.load_data()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/info")
def get_info():
    summary = engine.get_summary()
    return jsonify({"status": "success", "data": summary})

@app.route("/api/ohlc")
def get_ohlc():
    timeframe = request.args.get("tf", "15m")
    start_date = request.args.get("start", None)
    end_date = request.args.get("end", None)

    try:
        data = engine.get_formatted_ohlc(timeframe=timeframe, start_date=start_date, end_date=end_date)
        return jsonify({
            "status": "success",
            "timeframe": timeframe,
            "count": len(data),
            "data": data
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/api/indicators/list")
def list_indicators():
    available = IndicatorRegistry.get_all()
    return jsonify({"status": "success", "indicators": available})

@app.route("/api/indicators/calculate", methods=["POST"])
def calculate_indicator():
    payload = request.get_json() or {}
    indicator_name = payload.get("name")
    timeframe = payload.get("timeframe", "15m")
    params = payload.get("params", {})
    start_date = payload.get("start", None)
    end_date = payload.get("end", None)

    if not indicator_name:
        return jsonify({"status": "error", "message": "Indicator name is required."}), 400

    try:
        df = engine.get_ohlc(timeframe=timeframe, start_date=start_date, end_date=end_date)
        result = IndicatorRegistry.calculate(indicator_name, df, params)
        return jsonify({
            "status": "success",
            "timeframe": timeframe,
            "indicator": result
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    print("=================================================================")
    print("TradingView Lightweight Charts Server — MULTIBANK Parquet Engine")
    print("=================================================================")
    engine.load_data()
    app.run(host="0.0.0.0", port=5000, debug=True)
