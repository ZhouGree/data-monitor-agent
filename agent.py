"""
D17 决策层: 数据分析 Agent

三层骨架和第一周/第二周完全一致, 变化的只有 SYSTEM_PROMPT:
从"客服"换成"数据分析师", 把调查方法写进提示词, 不写一行 if 路由代码
"""

import os
from openai import OpenAI

try:
    from dotenv import load_dotenv
    load_dotenv()                        # 读 .env 里的 DEEPSEEK_API_KEY
except ImportError:
    pass

from tools import tools, execute_tool

SYSTEM_PROMPT = """你是电商平台的数据分析师, 任务是调查监控规则发现的异常事件。

调查方法:
1. 先调用 query_reviews 查看事件日期区间内该品类的用户评论, 从投诉内容中找线索。
2. 需要对比正常时期表现时, 调用 query_sales 查询(对比区间由你自行决定)。
3. 结论必须基于工具查到的真实数据, 关键结论要引用评论原话作证据, 严禁编造。
4. 查证充分后, 按以下格式输出报告, 不再调用工具:

【异常归因】{品类} {异常类型}
【根本原因】一句话说清
【数据证据】列出查到的关键数据和评论原话
【建议动作】1-2 条可执行建议"""

MAX_TOOL_ROUNDS = 5          # 保险丝: 防止 AI 反复查数死循环烧 token
REQUEST_TIMEOUT = 30         # 单次请求超时(秒), 防止网络卡死把整轮拖住


class MonitorAgent:
    def __init__(self):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("未找到 DEEPSEEK_API_KEY, 请复制 .env.example 为 .env 并填入 key")
        # max_retries=0: 重试交给 analyze.py 的 investigate(), 那里有统一的
        # 失败隔离/等待/汇总; 两层各重试一次会放大等待时间和重复计费。
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com",
                             timeout=REQUEST_TIMEOUT, max_retries=0)
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def chat(self, user_input: str) -> str:
        """一次调查: AI 可调 0~5 次工具取证, 最后返回归因报告"""
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=self.messages,      # 每次发完整历史
                tools=tools                  # 附带工具说明书
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:           # AI 查证完毕 -> 报告就是最终回答
                self.messages.append({"role": "assistant", "content": msg.content})
                return msg.content

            self.messages.append(msg)
            for call in msg.tool_calls:
                print(f"  [取证] {call.function.name}({call.function.arguments})")
                result = execute_tool(call.function.name, call.function.arguments)
                self.messages.append({
                    "role": "tool",          # 工具结果固定用这个 role
                    "tool_call_id": call.id,
                    "content": result
                })

        return "调查轮次超限, 请人工复核该事件。"
