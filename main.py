
import os
import time
import random
from dotenv import load_dotenv
from live_broker import LiveBroker
from base import OrderIntent

# 加载.env环境变量
load_dotenv()
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")

# ================== 策略配置 ==================
PAIR = "SOL/USD"       # 交易标的
ORDER_SIZE = 0.01      # 小额订单，不要调大
INTERVAL_SECONDS = 8   # 下单间隔
MAX_POSITION = 0.5     # SOL最大持仓上限
MAX_RPM = 30           # API最大请求速率
TOLERANCE = 0.005      # 价格容差
# ==============================================

def simple_activity_bot():
    broker = LiveBroker(API_KEY, SECRET_KEY, BASE_URL, MAX_RPM, TOLERANCE)
    print("===== 活跃度机器人启动 =====")
    print(f"交易对: {PAIR}, 单次下单量: {ORDER_SIZE}, 下单间隔: {INTERVAL_SECONDS}s")
    print("Ctrl + C 随时终止程序\n")

    try:
        while True:
            # 获取行情
            ticker = broker.get_ticker(PAIR)
            if not ticker:
                print("获取行情失败，等待下一轮...")
                time.sleep(INTERVAL_SECONDS)
                continue
            price = float(ticker["price"])
            print(f"\n【{time.ctime()}】 {PAIR} 当前价格: {price}")

            # 查询账户余额
            balance = broker.get_balance()
            if not balance:
                print("获取账户余额失败，等待下一轮...")
                time.sleep(INTERVAL_SECONDS)
                continue
            sol_free = float(balance.get("SOL", {}).get("free", 0))
            print(f"当前SOL可用持仓: {sol_free}")

            # 随机选择 BUY / SELL，增加交易多样性
            side = random.choice(["BUY", "SELL"])

            # 风控：如果准备买入，且持仓已经超过上限，强制改成卖出
            if side == "BUY" and sol_free >= MAX_POSITION:
                print(f"持仓{sol_free}超过上限{MAX_POSITION}，改为SELL")
                side = "SELL"

            # 设置限价单价格
            if side == "BUY":
                limit_price = round(price * 0.999, 2)
            else:
                limit_price = round(price * 1.001, 2)

            # ✅ 构建 OrderIntent 对象（适配live_broker接口）
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
