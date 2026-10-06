import os
import time
import random
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from live_broker import LiveBroker
from base import OrderIntent
# 导入队员实现的4个因子
from partB.factors import mom_vol_adj, short_reversal, low_vol, range_position

# =====================策略参数（团队可按需修改）=====================
load_dotenv()
API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")

PAIR = "SOL/USD"
ORDER_SIZE = 0.01
INTERVAL_SECONDS = 8
MAX_POSITION = 0.5          # 净持仓上限，最多+0.5多头 / -0.5空头
MAX_RPM = 30
TOLERANCE = 0.005
LOOKBACK_BARS = 30
SIGNAL_THRESHOLD = 0.002    # 单个因子的信号阈值
VOTE_THRESHOLD = 2          # 需要至少2个因子同向才下单
# ==================================================================

def multi_factor_vote_strategy():
    broker = LiveBroker(API_KEY, SECRET_KEY, BASE_URL, MAX_RPM, TOLERANCE)
    print("===== APAC Quant Hackathon｜4因子投票双向策略 =====")
    print("因子：short_reversal / mom_vol_adj / low_vol / range_position")
    print(f"投票规则：≥{VOTE_THRESHOLD}个因子同向，触发交易；支持裸空双向开仓")
    print("Ctrl + C 终止机器人\n")

    local_net_pos = 0.0  # 净持仓：正数多头，负数空头
    history = pd.DataFrame(columns=["ts", "price"])

    try:
        while True:
            # 获取盘口行情
            ticker = broker.get_ticker(PAIR)
            if not ticker or not ticker.get("Success"):
                print("行情API获取失败，等待下一轮")
                time.sleep(INTERVAL_SECONDS)
                continue

            data = ticker.get("Data", {})
            pair_data = data.get(PAIR)
            if not pair_data:
                print(f"未找到交易对 {PAIR}")
                time.sleep(INTERVAL_SECONDS)
                continue

            last_price = float(pair_data["LastPrice"])
            max_bid = float(pair_data["MaxBid"])
            min_ask = float(pair_data["MinAsk"])

            ts = pd.Timestamp.now()
            print(f"\n【{time.ctime()}】 SOL/USD Last={last_price} | Bid={max_bid} | Ask={min_ask}")
            print(f"【本地净持仓】SOL: {local_net_pos:.3f} （+多头 / -空头）")

            # 追加最新价格，保存K线历史
            new_row = pd.DataFrame([{"ts": ts, "price": price}])
            history = pd.concat([history, new_row], ignore_index=True)
            history = history.tail(LOOKBACK_BARS + 5)

            # 历史K线数量不足，不计算因子
            if len(history) < LOOKBACK_BARS:
                print(f"K线不足({len(history)}/{LOOKBACK_BARS})，继续采集，暂不下单")
                time.sleep(INTERVAL_SECONDS)
                continue

            close_series = history["price"]
            # 运行队员写好的4个因子
            rev_signal = short_reversal(close_series, LOOKBACK_BARS).iloc[-1]
            mom_signal = mom_vol_adj(close_series, LOOKBACK_BARS).iloc[-1]
            lowvol_signal = low_vol(close_series, LOOKBACK_BARS).iloc[-1]
            rng_signal = range_position(close_series, LOOKBACK_BARS).iloc[-1]

            factor_signals = [rev_signal, mom_signal, lowvol_signal, rng_signal]
            print(f"因子明细｜反转:{rev_signal:.4f} 动量:{mom_signal:.4f} 低波动:{lowvol_signal:.4f} 区间:{rng_signal:.4f}")

            # 统计投票
            vote_buy = 0
            vote_sell = 0
            for s in factor_signals:
                if s < -SIGNAL_THRESHOLD:
                    vote_buy += 1
                elif s > SIGNAL_THRESHOLD:
                    vote_sell += 1

            print(f"投票结果：看多票数={vote_buy}｜看空票数={vote_sell}")

            side = None
            if vote_buy >= VOTE_THRESHOLD:
                side = "BUY"
            elif vote_sell >= VOTE_THRESHOLD:
                side = "SELL"
            else:
                print("票数不足，无交易，跳过本轮\n")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 双向风控：检查下单后净持仓不超过±0.5
            if side == "BUY":
                new_net_pos = local_net_pos + ORDER_SIZE
            else:
                new_net_pos = local_net_pos - ORDER_SIZE

            if abs(new_net_pos) > MAX_POSITION:
                print(f"风控拦截：下单后净持仓{new_net_pos:.2f}，超出±{MAX_POSITION}上限，跳过订单\n")
                time.sleep(INTERVAL_SECONDS)
                continue

            # 限价单价格
            if side == "BUY":
                limit_price = round(max_bid * 1.0005, 2)
            else:
                limit_price = round(min_ask * 0.9995, 2)

            # 提交比赛订单
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
                print(f"✅ {side} 订单提交成功｜OrderID: {order_id}")
                local_net_pos = new_net_pos
            else:
                print(f"❌ {side} 订单提交失败｜详情: {order_resp.raw_response}")

            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n🤖 机器人手动停止")
    except Exception as e:
        print(f"\n💥 程序异常：{e}")

if __name__ == "__main__":
    multi_factor_vote_strategy()
