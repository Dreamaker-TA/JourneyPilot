"""Task prompt for the typed destination Research Packet worker."""

TASK_TEMPLATE = """Research the requested destination candidates.

Current task: {task_desc}
Original user request: {user_query}

{rag_context_section}

Use the available external tools where current facts are required. Return only
the ResearchPacket JSON object required by the system prompt.
"""

# 知识库 chunk 的第二个用途，也是本轮要立起来的那个：**提名**。
#
# 在这段话之前，提示词只把 chunk 说成「参考知识库」，唯一点名的用途是填描述性字段
# （见 research_packet_prompt 的 hard_contract）。于是五轮真跑里模型读了 chunk、
# 一个地名都没拿去解析：`RAG place funnel` 量到 mentions 一百多、queried 只有 2。
# 落差不在管道（管道与注入侧都查过、也量过），在于**没有任何一句话
# 告诉模型 chunk 里的地名值得去解析身份**。
#
# 边界一个字没松：知识库只提供**名字**，place_id / provider_place_type /
# provider_country_code 仍然逐字来自 global_place_search 的结果，`place_id` 的
# schema enum 不拆。知识库提名的地点与预检发现的地点因此是**同一种
# 东西**——都是 Provider 解析出来的身份，一起进 eligible_place_options 由模型挑。
#
# 「用原名去查」不是措辞洁癖：实测把名字和罗马音拼成一串（`浅草寺 Senso-ji`）时
# Provider 直接零命中，而同一个地方用 `浅草寺` 查得到。
KNOWLEDGE_BASE_NOMINATION = """参考知识库里出现的**具体地点**（寺庙、园林、博物馆、街区、老字号门店这类能安排进某一天的停留点）值得优先解析身份：
  - 从中挑出最具体、最贴合本轮任务与用户偏好的 3 至 5 个，逐个调用 global_place_search 解析。「杭州」「江南」「关东」这类区域名不用查。
  - 查询串就用知识库里写的那个名字本身，必要时只补一个城市名；不要把名字和外文译名拼成一串，也不要拼成描述性短语——Provider 按名字检索，拼串会直接零命中。
  - 解析出来的地点与服务器预检已经注入的地点**平等竞争**：它们一起构成本轮的 eligible_place_options，按任务和约束择优选用，不分先后、也不是只在别处找不到时才用。
  - 知识库只负责提名，不发身份：候选的 place_id、provider_place_type、provider_country_code 仍必须逐字来自 global_place_search 的结果。知识库里写的名字本身不能直接写成候选。"""

# 餐饮选项的软合同。服务端在预检里已经用 Provider 解析出具体门店身份并把信封注入本轮
# 消息；旧提示词一个字都没提这件事，模型因此合法地一条 DiningCandidate 都不写，交付里
# 的餐饮全是编排期自撰（P17 北京→曼谷走查）。
#
# 这里列的是本轮服务端给出的**全部**餐厅，不只是评价核得上的那些。评价核验
# 是加分项：核上了报告里带「外部评价已核验」，核不上照常给出、不带标记，而这件事由服务端
# 按信封判定，不由模型判定——所以不能在这里先把没核上的筛掉。
#
# 这段只约束「哪些餐厅可以成为候选」，不承诺一定要有餐饮。
DINING_OPTIONS_TEMPLATE = """
本轮可选的餐饮门店：{place_ids}
- 这些 place_id 由服务器用 Provider 解析出具体门店身份并注入本轮消息；请逐条写出对应的 DiningCandidate，place_id 逐字照抄，不重新解析、不改写。
- 不要自造餐厅候选：上述 place_id 之外的餐馆、市场、商圈、美食街一律不得写进候选。
- 不必自行判断这些门店的口碑或质量，也不要因为找不到评价就跳过某一家；是否标注「已核验」由服务器决定。
- 一条都没有时就不写 DiningCandidate，缺少餐饮不算本轮失败。"""

NO_DINING_OPTIONS = "本轮没有（服务器未解析出任何具体餐厅门店）"
