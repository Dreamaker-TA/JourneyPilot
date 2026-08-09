"""Task prompt for the typed transport Research Packet worker."""

TASK_TEMPLATE = """请调研当前交通任务。首次完整调研只能使用 12306/航班等长距离 Provider，
核验受控出发地到目的地的城际班次、时刻和价格；此时 placement skeleton 尚未形成，
禁止研究目的地市内路线、附近车站、机场到景点或任意实体对，即使 Planner 文本提到了市内交通也必须忽略。
市内衔接只能留待系统从 placement skeleton 提取精确相邻端点后，再由独立 connector gap 轮次研究。

当前任务：{task_desc}
用户原始需求：{user_query}

如果且仅如果用户明确要求“跨日 / 跨夜 / overnight / red-eye”长途交通，调用航班 Provider 时必须传
require_cross_day=true；没有符合条件的真实 Provider 路线就明确失败。不得改写 Provider 时刻、复制旧候选
或用“夜间”文字把同日路线伪装成跨日。普通请求不得设置该参数。

只有 connector gap 轮次才存在指定 endpoint pair；届时必须使用真实路线 Provider，并逐字保留 Provider endpoints。
只返回系统提示规定的 ResearchPacket JSON 对象。
"""
