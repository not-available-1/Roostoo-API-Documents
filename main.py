import os
import time
import random
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from live_broker import LiveBroker
from base import OrderIntent
# 导入因子模块
from partB.factors import mom_vol_adj, short_reversal, low_vol, range_position

# 加载.env环境变量
load_dotenv()
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")
# ================== 策略配置 ==================
PAIR = "SOL/USD"       # 交易标的
ORDER_SIZE = 0.01      # 单次下单数量
INTERVAL_SECONDS = 8   # 循环间隔
MAX_POSITION = 0.5     # SOL持仓上限
MAX_RPM = 30           # API请求限速
TOLERANCE = 0.005      # 价格容差
LOOKBACK_BARS = 30     # 回看K线数量
THRESHOLD = 0.002      # 因子触发阈值
# ==============================================

def simple_activity_bot():
    broker = LiveBroker(API_KEY, SECRET_KEY, BASE_URL, MAX_RPM, TOLERANCE)
    print("===== 因子量化机器人启动 =====")
    print(f"交易对: {PAIR}, 单次下单量: {ORDER_SIZE}, 下单间隔: {INTERVAL_SECONDS}s")
    print("Ctrl + C 随时终止程序\n")

    # 本地内存维护持仓，不调用余额API
    local_pos = 0.0
    # 初始化历史行情缓存
    history = pd.DataFrame(columns=["ts", "price"])

    try:
        while True:
            # 获取行情Ticker
            ticker = broker.get_ticker(PAIR)
            if not ticker or not ticker.get("Success"):
                print("获取行情失败，等待下一轮... ticker resp:", ticker)
                time.sleep(INTERVAL_SECONDS)
                continue

            data = ticker.get("Data", {})
            pair_data = data.get(PAIR)
            if not pair_data:
                print(f"返回数据找不到交易对 {PAIR}")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 读取盘口+成交价
            last_price = float(pair_data["LastPrice"])
            max_bid = float(pair_data["MaxBid"])
            min_ask = float(pair_data["MinAsk"])

            price = last_price
            ts = pd.Timestamp.now()
            print(f"\n【{time.ctime()}】 {PAIR} Last={last_price} | Bid={max_bid} | Ask={min_ask}")
            print(f"【本地跟踪持仓】SOL: {local_pos}")

            # 保存K线历史
            new_row = pd.DataFrame([{"ts": ts, "price": price}])
            history = pd.concat([history, new_row], ignore_index=True)
            history = history.tail(LOOKBACK_BARS + 5)

            # 收集够30根K线才计算因子
            if len(history) < LOOKBACK_BARS:
                print(f"历史K线不足({len(history)}/{LOOKBACK_BARS})，继续采集，暂不下单")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 短期反转因子计算
            close_series = history["price"]
            rev_signal = short_reversal(close_series, LOOKBACK_BARS).iloc[-1]
            print(f"短期反转因子信号: {rev_signal:.4f}")

            # =========策略信号逻辑：先买后卖闭环=========
            side = None
            if rev_signal < -THRESHOLD:
                side = "BUY"
            elif rev_signal > THRESHOLD:
                side = "SELL"
            else:
                print("因子信号在阈值区间，无交易，跳过本轮")
                time.sleep(INTERVAL_SECONDS)
                continue

            # ===== 风控规则 =====
            if side == "BUY":
                # 做多：不能超过最大持仓上限
                if local_pos + ORDER_SIZE > MAX_POSITION:
                    print(f"开仓会超过最大持仓上限{MAX_POSITION}，跳过买单")
                    time.sleep(INTERVAL_SECONDS)
                    continue
                # 买单限价：高于买一一点点，提升成交概率
                limit_price = round(max_bid * 1.0005, 2)

            elif side == "SELL":
                # 做空：**必须本地持仓>0才允许卖，没有持仓禁止发卖单**
                if local_pos < ORDER_SIZE:
                    print(f"本地持仓不足{ORDER_SIZE}，无法卖出，跳过卖单")
                    time.sleep(INTERVAL_SECONDS)
                    continue
                # 卖单限价：低于卖一一点点，提升成交概率
                limit_price = round(min_ask * 0.9995, 2)

            # 提交限价订单
            order_intent = OrderIntent(
                pair=PAIR,
                side=side,
                order_type="LIMIT",
                price=limit_price,
                quantity=ORDER_SIZE
            )
            order_resp = broker.place_order(order_intent)
            if order_resp.success:
                order_id = order_resp.order_id
                print(f"✅ {side}下单成功！订单ID: {order_id}")
                # 下单成功，更新本地持仓计数
                if side == "BUY":
                    local_pos += ORDER_SIZE
                else:
                    local_pos -= ORDER_SIZE
            else:
                print(f"❌ {side}下单失败，返回: {order_resp.raw_response}")

            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n🤖 机器人停止")
    except Exception as e:
        print(f"\n💥 程序异常：{e}")

if __name__ == "__main__":
    simple_activity_bot()
