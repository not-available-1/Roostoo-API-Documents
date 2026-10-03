# main.py 读取.env版本，密钥不硬编码，用于Github和AWS
import os
import time
from dotenv import load_dotenv

# 读取本地 .env 文件，密钥放在 .env，不写在代码里
load_dotenv()

API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")
MAX_RATE_PER_MINUTE = int(os.getenv("MAX_RATE_PER_MINUTE"))
TIME_OFFSET_TOLERANCE = int(os.getenv("TIME_OFFSET_TOLERANCE"))

from live_broker import LiveBroker
from base import OrderIntent

def main():
    print("=== Bot 启动，开始时钟校准 ===")
    broker = LiveBroker(
        api_key=API_KEY,
        secret_key=SECRET_KEY,
        base_url=BASE_URL,
        max_rpm=MAX_RATE_PER_MINUTE,
        tolerance=TIME_OFFSET_TOLERANCE
    )

    server_ts = broker.get_server_time()
    local_ts = int(time.time() * 1000)
    delta_ms = abs(server_ts - local_ts)
    print(f"服务器时间戳: {server_ts}")
    print(f"本地机器时间戳: {local_ts}")
    print(f"时间差(毫秒): {delta_ms}")

    if delta_ms > broker.tolerance_ms:
        print(f"时间偏差过大 {delta_ms}ms，程序直接退出！")
        return

    print("时钟校验通过，进入主循环")

    while True:
        try:
            bal = broker.get_balance()
            print("\n【账户信息】", bal)

            # ====== 去掉下面两行前面 # 号就会自动下单，提高交易活跃度 ======
            # test_order = OrderIntent(
            #     pair="BNB/USD",
            #     side="BUY",
            #     quantity=0.01,
            #     price=None,
            #     order_type="MARKET",
            #     reason="test‑activate‑trade"
            # )
            # res = broker.place_order(test_order)
            # print("下单返回：", res)

        except Exception as err:
            print("捕获异常:", err)

        time.sleep(12)

if __name__ == "__main__":
    main()
