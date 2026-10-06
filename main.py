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
ORDER_SIZE = 0.01      # 小额订单
INTERVAL_SECONDS = 8   # 下单间隔
MAX_POSITION = 0.5     # SOL最大持仓上限
MAX_RPM = 30           # API最大请求速率
TOLERANCE = 0.005      # 价格容差
LOOKBACK_BARS = 30     # 因子回看窗口
THRESHOLD = 0.002      # 因子信号阈值
# ==============================================

def simple_activity_bot():
    broker = LiveBroker(API_KEY, SECRET_KEY, BASE_URL, MAX_RPM, TOLERANCE)
    print("===== 因子量化机器人启动 =====")
    print(f"交易对: {PAIR}, 单次下单量: {ORDER_SIZE}, 下单间隔: {INTERVAL_SECONDS}s")
    print("Ctrl + C 随时终止程序\n")

    # 初始化历史行情缓存
    history = pd.DataFrame(columns=["ts", "price"])

    try:
        while True:
            # 获取行情
            ticker = broker.get_ticker(PAIR)
            # 适配mock-api /v3/ticker嵌套返回结构
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

            # 读取接口真实返回字段：LastPrice MaxBid MinAsk
            last_price = float(pair_data["LastPrice"])
            max_bid = float(pair_data["MaxBid"])
            min_ask = float(pair_data["MinAsk"])

            price = last_price
            ts = pd.Timestamp.now()
            print(f"\n【{time.ctime()}】 {PAIR} Last={last_price} | Bid={max_bid} | Ask={min_ask}")

            # 追加新行情到历史缓存
            new_row = pd.DataFrame([{"ts": ts, "price": price}])
            history = pd.concat([history, new_row], ignore_index=True)
            # 只保留最近LOOKBACK_BARS条，控制内存
            history = history.tail(LOOKBACK_BARS + 5)

            # 查询账户余额
           sol_free = 0.0
print(f"【临时模拟】SOL可用持仓: {sol_free}")


            # 判断历史数据是否足够计算因子
            if len(history) < LOOKBACK_BARS:
                print(f"历史数据不足({len(history)}/{LOOKBACK_BARS})，继续收集行情，跳过本次下单")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 提取价格序列，计算短期反转因子
            close_series = history["price"]
            rev_signal = short_reversal(close_series, LOOKBACK_BARS).iloc[-1]
            print(f"短期反转因子信号: {rev_signal:.4f}")

            # 生成交易信号
            if rev_signal > THRESHOLD:
                side = "SELL"
            elif rev_signal < -THRESHOLD:
                side = "BUY"
            else:
                print("因子信号不足，无交易，跳过本轮")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 风控：如果准备买入，且持仓已经超过上限
            if side == "BUY" and sol_free >= MAX_POSITION:
                print(f"持仓{sol_free}超过上限{MAX_POSITION}，改为SELL")
                side = "SELL"

            # 使用盘口价格设置限价，提高成交概率
            if side == "BUY":
                limit_price = round(max_bid * 1.0005, 2)
            else:
                limit_price = round(min_ask * 0.9995, 2)

            # 构建 OrderIntent 对象
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
                print(f"✅ {side} 下单成功！订单ID: {order_id}")
            else:
                print(f"❌ 下单失败，返回信息: {order_resp.raw_response}")
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n\n🤖 收到终止信号，机器人停止运行。")
    except Exception as e:
        print(f"\n💥 程序异常：{e}")

if __name__ == "__main__":
    simple_activity_bot()
