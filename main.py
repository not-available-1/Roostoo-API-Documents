import os
import time
import random
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from live_broker import LiveBroker
from base import OrderIntent
# 直接导入队员写好的全部因子
from partB.factors import mom_vol_adj, short_reversal, low_vol, range_position

# 加载.env环境变量
load_dotenv()
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")
# ================== 策略配置（可按你们团队回测结果修改） ==================
PAIR = "SOL/USD"       # 交易标的
ORDER_SIZE = 0.01      # 单次下单数量
INTERVAL_SECONDS = 8   # 循环间隔
MAX_POSITION = 0.5     # 净持仓绝对值上限 ±0.5
MAX_RPM = 30           # API请求限速
TOLERANCE = 0.005      # 价格容差
LOOKBACK_BARS = 30     # 回看K线数量
SIGNAL_THRESHOLD = 0.002 # 单个因子触发阈值
VOTE_THRESHOLD = 2     # 需要至少2个因子达成一致，才下单
# ========================================================================

def multi_factor_vote_bot():
    broker = LiveBroker(API_KEY, SECRET_KEY, BASE_URL, MAX_RPM, TOLERANCE)
    print("===== 多因子投票量化机器人【队员因子版本｜双向裸空】 =====")
    print(f"交易对: {PAIR}, 单次下单量: {ORDER_SIZE}, 下单间隔: {INTERVAL_SECONDS}s")
    print("因子列表：short_reversal, mom_vol_adj, low_vol, range_position")
    print(f"投票规则：至少{VOTE_THRESHOLD}个因子同向，才触发交易\nCtrl + C 随时终止程序\n")

    local_net_pos = 0.0 # 净持仓，正数多头，负数空头
    history = pd.DataFrame(columns=["ts", "price"])

    try:
        while True:
            # 获取行情
            ticker = broker.get_ticker(PAIR)
            if not ticker or not ticker.get("Success"):
                print("获取行情失败，等待下一轮...")
                time.sleep(INTERVAL_SECONDS)
                continue

            data = ticker.get("Data", {})
            pair_data = data.get(PAIR)
            if not pair_data:
                print(f"找不到交易对 {PAIR}")
                time.sleep(INTERVAL_SECONDS)
                continue

            last_price = float(pair_data["LastPrice"])
            max_bid = float(pair_data["MaxBid"])
            min_ask = float(pair_data["MinAsk"])

            price = last_price
            ts = pd.Timestamp.now()
            print(f"\n【{time.ctime()}】 {PAIR} Last={last_price} | Bid={max_bid} | Ask={min_ask}")
            print(f"【本地净持仓】SOL: {local_net_pos:.3f} （+多头 / -空头）")

            # 保存K线
            new_row = pd.DataFrame([{"ts": ts, "price": price}])
            history = pd.concat([history, new_row], ignore_index=True)
            history = history.tail(LOOKBACK_BARS + 5)

            # K线不足，不计算因子
            if len(history) < LOOKBACK_BARS:
                print(f"历史K线不足({len(history)}/{LOOKBACK_BARS})，继续采集，暂不下单")
                time.sleep(INTERVAL_SECONDS)
                continue

            close_series = history["price"]
            # 调用队员写好的4个因子函数
            rev_signal = short_reversal(close_series, LOOKBACK_BARS).iloc[-1]
            mom_signal = mom_vol_adj(close_series, LOOKBACK_BARS).iloc[-1]
            lowvol_signal = low_vol(close_series, LOOKBACK_BARS).iloc[-1]
            rng_signal = range_position(close_series, LOOKBACK_BARS).iloc[-1]

            # 存入因子列表
            factor_signals = [rev_signal, mom_signal, lowvol_signal, rng_signal]
            print(f"因子明细｜反转:{rev_signal:.4f} 动量:{mom_signal:.4f} 低波动:{lowvol_signal:.4f} 区间:{rng_signal:.4f}")

            # 统计看多、看空票数
            vote_buy = 0
            vote_sell = 0
            for s in factor_signals:
                if s < -SIGNAL_THRESHOLD:
                    vote_buy +=1
                elif s > SIGNAL_THRESHOLD:
                    vote_sell +=1

            print(f"投票统计：看多票数={vote_buy}，看空票数={vote_sell}")

            side = None
            if vote_buy >= VOTE_THRESHOLD:
                side = "BUY"
            elif vote_sell >= VOTE_THRESHOLD:
                side = "SELL"
            else:
                print("因子投票不足，无交易，跳过本轮")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 双向风控：控制净持仓绝对值上限
            if side == "BUY":
                new_net_pos = local_net_pos + ORDER_SIZE
            else:
                new_net_pos = local_net_pos - ORDER_SIZE

            if abs(new_net_pos) > MAX_POSITION:
                print(f"下单后净持仓{new_net_pos:.2f}，超过±{MAX_POSITION}上限，跳过订单")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 设置限价
            if side == "BUY":
                limit_price = round(max_bid * 1.0005, 2)
            else:
                limit_price = round(min_ask * 0.9995, 2)

            # 提交订单
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
                local_net_pos = new_net_pos
            else:
                print(f"❌ {side}下单失败，返回: {order_resp.raw_response}")

            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n🤖 机器人停止")
    except Exception as e:
        print(f"\n💥 程序异常：{e}")

if __name__ == "__main__":
    multi_factor_vote_bot()
