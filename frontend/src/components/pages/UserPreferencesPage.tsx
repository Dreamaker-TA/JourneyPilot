import React, { useEffect, useState } from 'react';
import { AnimatePresence } from 'motion/react';
import { Clock3, Eraser, Plus, Save, Trash2 } from 'lucide-react';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { ConfirmAction } from '../ui/ConfirmAction';
import { Input } from '../ui/Input';
import { Modal } from '../ui/Modal';
import { PageShell, type SurfaceState } from '../ui/PageShell';
import { Chip } from '../ui/Chip';
import { RuledList, RuledRow } from '../ui/RuledList';
import { GROUP_LABEL, SECTION_NOTE, SECTION_TITLE } from '../../lib/typography';
import { useApp } from '../../context/AppContext';
import { api } from '../../lib/api';
import { describeRequestFailure, type RequestFailure } from '../../lib/requestFailureMessage';
import {
  buildPreferencePayload,
  preferenceChipGroups,
  readPreferenceSelections,
  togglePreferenceOption,
  type PreferenceSelections,
} from '../../lib/preferenceGroups';
import { ChartMark } from '../ui/ChartMark';
import type { MemoryFactItem, PreferenceOptionGroup } from '../../types/api';
import type { PlaceIdentity } from '../../types/api';
import { PlaceField } from '../trip/TripPlanner';

/**
 * 记忆分类的中文映射（记忆列表分类 tag 用）。
 *
 * **只列产品真能产出的四类**：抽取器的 `ExtractedFactCandidate.category` 是
 * `trip_plan | preference | constraint | feedback` 四值枚举，手动添加走后端默认
 * `preference`。别的分类没有任何写入方 —— 一张为不存在的取值准备的表，读起来像是
 * 有人在产出它们。未知分类由 `memoryCategoryLabel` 原样显示，不必在这里兜底。
 */
const memoryCategoryLabels: Record<string, string> = {
  preference: '偏好',
  constraint: '约束',
  trip_plan: '行程',
  feedback: '反馈',
};

function memoryCategoryLabel(category: string | null): string | null {
  if (!category) return null;
  return memoryCategoryLabels[category] || category;
}

/**
 * 区块之间那条发丝线。
 *
 * 区块间距是 **40px**，中间画一条发丝线。组与组之间 69px（`py-4` + 内容）而区块之间
 * 只有 24px 的节奏是**倒的** —— 该在一起的东西比不该在一起的东西离得更远，于是三个
 * 区块读起来是一片连续的行。
 *
 * 「inner 14px / section 24px，一个角色一个值」的用意保留（不给范围），但 24 这个数是
 * 在**没有区块标题层级**的前提下定的；标题上到 20px 之后，24px 的上下留白撑不住它。
 */
const SECTION_RULE = 'border-t border-stroke/60 pt-10';

export const UserPreferencesPage: React.FC = () => {
  const { state } = useApp();
  /**
   * 选项表整份来自服务端（`GET /api/users/preference-options`）。
   *
   * **这一屏不许自己带一份。** 后端 `TravelPreference` 只收表里的值，所以「存下来的值
   * 一定画得出来」这条保证由那一份表给；再抄一份两份表就会漂开 —— 一个存在的值会变成
   * 零枚高亮的 chip，多选组还永远点不掉它。
   */
  const [groups, setGroups] = useState<PreferenceOptionGroup[]>([]);
  const [selections, setSelections] = useState<PreferenceSelections>({});
  const [facts, setFacts] = useState<MemoryFactItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newMemory, setNewMemory] = useState('');
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  /**
   * 「删除全部记忆」的确认弹层。
   *
   * 破坏性操作分三级，整库 / 整集销毁是 T3 —— 走 Modal，标题陈述后果。
   * **不要降成 T2（就地二段确认）**：隔壁「资料来源」屏上同一级别的「删除资料库」是
   * Modal，同一级操作两种确认方式，读者在两屏之间学到的东西会互相矛盾。
   * 单条记忆的删除是 T2（ConfirmAction）—— 那是单对象删除，级别本来就不同。
   */
  const [showDeleteAllModal, setShowDeleteAllModal] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [factsUnavailable, setFactsUnavailable] = useState(false);
  const [defaultOrigin, setDefaultOrigin] = useState<PlaceIdentity | null>(null);
  const [savingOrigin, setSavingOrigin] = useState(false);
  /**
   * 失败态走 `describeRequestFailure` + `RequestFailureNotice`，四个侧屏同一份。
   *
   * **不许**在 `catch` 里写死句子（「暂时没有取到记忆数据。」「删除失败。请稍后重试。」那类）：
   * 那是**一句手写的句子替自己决定了可重试性** —— A screen that decides retryability by hand
   * decides it differently from the screen next to it。恢复动作和句子必须一起算出来。
   */
  const [loadError, setLoadError] = useState<RequestFailure | null>(null);

  const loadAll = async () => {
    // 身份未解析出来时 state.userId 是空串、也可能是 anonymous 哨兵，两种都不能去读
    // 用户私有数据面（空串会打成 /api/users//profile 这种空段路径）；以
    // userIdentityReady 为准，落定后本 effect 会重跑。
    if (!state.userIdentityReady) return;
    setLoading(true);
    setLoadError(null);
    try {
      const [optionsResult, profileResult, factsResult, originResult] = await Promise.allSettled([
        api.getPreferenceOptions(),
        api.getUserProfile(state.userId),
        api.listMemoryFacts(state.userId),
        api.getDefaultOrigin(state.userId),
      ]);
      // 选项表读不到 = 这一屏的偏好那一整块画不出来。**走失败态，不画空的 chip 排。**
      // 一排「全都没选」的 chip 在一个满是偏好的账户上是一句假话；说「读不到」比画一个
      // 看起来是真的空状态诚实。
      if (optionsResult.status === 'rejected') {
        setLoadError(describeRequestFailure(optionsResult.reason, '读取', '偏好选项'));
        setGroups([]);
        setSelections({});
        setFacts([]);
        setFactsUnavailable(true);
        return;
      }
      // 剩下三个请求全灭 = 这一屏读不到任何属于这个人的东西，同样走失败态。
      if (
        profileResult.status === 'rejected' &&
        factsResult.status === 'rejected' &&
        originResult.status === 'rejected'
      ) {
        setLoadError(describeRequestFailure(profileResult.reason, '读取', '你的偏好'));
        setGroups([]);
        setSelections({});
        setFacts([]);
        setFactsUnavailable(true);
        return;
      }
      setGroups(optionsResult.value);
      setSelections(
        readPreferenceSelections(
          optionsResult.value,
          profileResult.status === 'fulfilled' ? profileResult.value : null,
        )
      );
      if (factsResult.status === 'fulfilled') {
        setFacts(factsResult.value.facts);
        setFactsUnavailable(false);
      } else {
        setFacts([]);
        setFactsUnavailable(true);
      }
      if (originResult.status === 'fulfilled') setDefaultOrigin(originResult.value);
      if (profileResult.status === 'rejected' || factsResult.status === 'rejected') {
        setStatusMessage('部分记忆数据暂时不可用。');
      }
    } finally {
      setLoading(false);
    }
  };

  const refreshFacts = async () => {
    try {
      const factList = await api.listMemoryFacts(state.userId);
      setFacts(factList.facts);
      setFactsUnavailable(false);
    } catch (err) {
      setFactsUnavailable(true);
      setStatusMessage(describeRequestFailure(err, '读取', '记忆列表').message);
    }
  };

  useEffect(() => {
    void loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.userId, state.userIdentityReady]);

  const toggleOption = (group: PreferenceOptionGroup, option: string) => {
    // 单选 / 多选的规则与 payload 的形状都在 `lib/preferenceGroups.ts` 的纯函数里。
    setSelections((prev) => togglePreferenceOption(group, prev, option));
  };

  const handleSave = async () => {
    setSaving(true);
    setStatusMessage(null);
    try {
      await api.updatePreferences(state.userId, buildPreferencePayload(groups, selections));
      setStatusMessage('偏好已保存。');
    } catch (err) {
      setStatusMessage(describeRequestFailure(err, '保存', '你的偏好').message);
    } finally {
      setSaving(false);
    }
  };

  const handleAddMemory = async () => {
    const content = newMemory.trim();
    if (!content || adding) return;
    setAdding(true);
    setStatusMessage(null);
    try {
      const result = await api.addMemoryFact(state.userId, content);
      setNewMemory('');
      await refreshFacts();
      // 服务端对同一句话是幂等的。不说这一句，用户看到的就是「按了添加、
      // 输入框清空了、列表没变长」—— 一次成功读起来像一次没反应。
      if (result.status === 'existing') {
        setStatusMessage('这条记忆本来就在列表里了，没有重复添加。');
      }
    } catch (err) {
      setStatusMessage(describeRequestFailure(err, '添加', '这条记忆').message);
    } finally {
      setAdding(false);
    }
  };

  const handleDeleteFact = async (fact: MemoryFactItem) => {
    setBusy(true);
    setStatusMessage(null);
    try {
      await api.deleteMemoryFact(state.userId, fact.fact_id, {
        reason: `remove memory fact: ${fact.content}`,
      });
      await refreshFacts();
    } catch (err) {
      setStatusMessage(describeRequestFailure(err, '删除', '这条记忆').message);
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteAll = async () => {
    setBusy(true);
    setStatusMessage(null);
    setShowDeleteAllModal(false);
    try {
      await api.deleteAllMemory(state.userId, {
        reason: 'user requested full first-party memory deletion',
        clear_auto_portrait: true,
        clear_graph: true,
        clear_session_anchors: true,
      });
      setStatusMessage('已删除。');
      await loadAll();
    } catch (err) {
      setStatusMessage(describeRequestFailure(err, '删除', '全部记忆').message);
    } finally {
      setBusy(false);
    }
  };

  const handleRetentionCleanup = async () => {
    setBusy(true);
    setStatusMessage(null);
    try {
      const cleanup = await api.cleanupExpiredMemory(state.userId, {
        reason: 'manual retention cleanup from Memory Center',
        limit: 1000,
      });
      // 回执报真实条数，而不是替自己免责说「没有过期内容时不会删除任何记忆」——
      // 那是替一次操作作担保，而按钮上写着「清理过期」，用户点它就是要清理。
      setStatusMessage(
        cleanup.affected_facts > 0 ? `已清理 ${cleanup.affected_facts} 条过期记忆。` : '没有过期记忆。'
      );
      await refreshFacts();
    } catch (err) {
      setStatusMessage(describeRequestFailure(err, '删除', '过期记忆').message);
    } finally {
      setBusy(false);
    }
  };

  const surfaceState: SurfaceState = !state.userIdentityReady
    ? { kind: 'identity-unresolved' }
    : loading
      ? { kind: 'loading' }
      : loadError
        ? {
            kind: 'error',
            title: '暂时读不到你的偏好',
            failure: loadError,
            onRetry: () => void loadAll(),
          }
        : { kind: 'ready' };

  return (
    <>
    <PageShell
      title="我的偏好"
      purpose="规划时会默认按这里的口味来"
      identitySurface="我的偏好"
      state={surfaceState}
    >
      <div className="flex flex-col gap-10">
        {statusMessage && (
          <p className="rounded-card border border-stroke bg-panel px-3.5 py-2.5 text-[13px] leading-relaxed text-ink-secondary">
            {statusMessage}
          </p>
        )}

        {/* ── 常用出发地 ── */}
        <section>
          <h2 className={SECTION_TITLE}>常用出发地</h2>
          <p className={SECTION_NOTE}>只影响之后新建的旅行。</p>
          {/**
           * 字段**收到 `max-w-sm`**。它不能通栏：一条 900px 的刻线上印着「上海市」三个字，
           * 而那枚「常用」标飘在 800 多像素之外 —— 一个字段的度量应该
           * 由**它承的那个值**决定，不由列宽决定。
           *
           * 这不与「不设 max-w 免得右边空掉」矛盾：那句话说的是**整块**不要缩到 576px，
           * 而这里缩的是一格只承一个城市名的字段，它右边紧接着就是保存钮。
           */}
          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-end">
            <PlaceField
              role="origin"
              value={defaultOrigin}
              onChange={setDefaultOrigin}
              label="出发地"
              frequent
              className="sm:max-w-sm"
            />
            <Button
              variant="secondary"
              disabled={!defaultOrigin || savingOrigin}
              loading={savingOrigin}
              className="flex-shrink-0"
              onClick={async () => {
                if (!defaultOrigin) return;
                setSavingOrigin(true);
                try {
                  await api.setDefaultOrigin(state.userId, defaultOrigin);
                  setStatusMessage('常用出发地已更新。');
                } catch (err) {
                  setStatusMessage(describeRequestFailure(err, '保存', '常用出发地').message);
                } finally {
                  setSavingOrigin(false);
                }
              }}
            >
              保存出发地
            </Button>
          </div>
        </section>

        {/* ── 旅行偏好 ── */}
        <section className={SECTION_RULE}>
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className={SECTION_TITLE}>旅行偏好</h2>
              <p className={SECTION_NOTE}>选好这些常用偏好，规划时 JourneyPilot 会优先考虑。</p>
            </div>
            {/* 三个区块的动作**同重量**：常用出发地存一次、旅行偏好存一次、记忆加一条，
                三件事没有主次，所以不许一个 `secondary`、两个 `primary` —— 那既是同一个角色
                两套值，又会让一屏出现两个 accent 时刻（克制底线是一屏一个）。这一屏没有
                单一主动作，accent 只留给「被选中的偏好」那一个真状态。 */}
            <Button
              variant="secondary"
              size="sm"
              onClick={handleSave}
              loading={saving}
              className="flex-shrink-0"
            >
              <Save size={14} />
              保存
            </Button>
          </div>

          {/**
           * 六组偏好：**组名在自己那一行、选项在下一行通栏**。
           *
           * 「组名一列（96px）、选项一列」的两列刻线分组有三处硬伤：
           *
           * 1. **组名与选项同号同色**（都是 12px `ink-secondary`），于是「这是类目、那些是
           *    取值」这层结构一个通道都没表达出来 —— 等于没有标注。组名现在是 14/600 `ink`
           *    （「区块之内的区块标题」档），和 13px 的选项差一档字号、一档
           *    字重、一档墨色。
           * 2. **节奏是倒的**：组与组之间 69px，而三个大区块之间只有 24px。现在组间 28px、
           *    区块间 40px + 一条发丝线。
           * 3. 96px 的组名列把选项挤进右边 800px 里排，而组名最长四个字 —— 一列宽度为最长
           *    情况留白，其余五行各空掉一半。通栏之后每一组的选项从同一条左轴起排。
           *
           * 选项走 `ui/Chip`：未选中带描边（裸文字 30 枚会连成一片灰词，读不出可点），
           * 选中是满面淡底 + 描边 + 加粗。**仍然不做成六张卡** —— 那条理由没变：「一组偏好
           * 选项」不是一个**具体对象**，而卡会带上只给 major surface 的软阴影。
           */}
          <div className="mt-6 flex flex-col gap-7">
            {preferenceChipGroups(groups, selections).map((group, index) => (
              <div key={group.key} data-testid={`preference-group-${group.key}`}>
                <div className="flex items-baseline gap-2">
                  <h3 className={GROUP_LABEL}>{group.label}</h3>
                  {/* 单值字段是单选 —— 明示出来，别让用户只有点下去才知道。11px 读数档。
                      单选/多选也来自服务端那张表，不在这里第二次判断。 */}
                  {!group.multi && <span className="text-[11px] text-ink-muted">单选</span>}
                </div>
                <div className="mt-2.5 flex flex-wrap gap-2">
                  {group.chips.map(({ option, selected }) => (
                    <Chip
                      key={option}
                      selected={selected}
                      onClick={() => toggleOption(groups[index], option)}
                    >
                      {option}
                    </Chip>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* ── 个性记忆 ── */}
        <section className={SECTION_RULE}>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 className={SECTION_TITLE}>个性记忆</h2>
{/* 本文案是产品对这一层效力的**唯一**说明，它必须与后端真给的那一档对得上。
                   手写记忆在深度路径上进的是【本轮统一约束】里的偏好那一段
                   （`panels/constraint.py::_map_manual_memory_facts`，`type=soft`）：
                   模型会读到、会据此取舍，但门不据它拦截，与本轮提出的要求冲突时让位。 */}
              <p className={SECTION_NOTE}>
                这里的记忆会作为偏好带进每一次规划。与你本轮提出的要求冲突时，以本轮为准。
              </p>
            </div>
            <div className="flex flex-shrink-0 flex-wrap gap-2">
              <Button
                variant="ghost"
                size="sm"
                onClick={handleRetentionCleanup}
                disabled={busy || factsUnavailable}
              >
                <Clock3 size={14} />
                清理过期
              </Button>
{/* 整集销毁是 T3：走 Modal，标题陈述后果。
                   和「资料来源」屏的「删除资料库」同一副身体。

                   禁用条件里**不看 `facts.length`**：这枚钮删的不止下面这张列表 ——
                   系统总结出来的印象、印象之间的关联、各次对话的摘要都会一起清掉，而那三样
                   一个都不显示在这一屏上。「列表是空的」推不出「没有东西可删」。 */}
              <Button
                variant="ghost"
                size="sm"
                data-testid="memory-delete-all"
                onClick={() => setShowDeleteAllModal(true)}
                disabled={busy || factsUnavailable}
              >
                <Eraser size={14} />
                删除全部
              </Button>
            </div>
          </div>

          {/* 添加行：一条刻线字段 + 一枚钮。这一格承的是一句话，所以它通栏 ——
              与上面「常用出发地」收到 `max-w-sm` 不矛盾：那一格承的是一个城市名。 */}
          <div className="mt-6 flex w-full items-end gap-3">
            <div className="min-w-0 flex-1">
              <Input
                value={newMemory}
                onChange={(e) => setNewMemory(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void handleAddMemory();
                  }
                }}
                placeholder="例如：我只坐直飞航班"
              />
            </div>
            <Button
              variant="secondary"
              size="md"
              onClick={() => void handleAddMemory()}
              loading={adding}
              disabled={!newMemory.trim()}
              className="flex-shrink-0"
            >
              <Plus size={14} />
              添加
            </Button>
          </div>

          {facts.length === 0 ? (
            <div className="mt-6 flex flex-col items-center justify-center py-12 text-center">
              {/**
               * 空态是一枚**空白索引标签**：一套归档系统，里面还什么都没归进去。
               *
* **不用** lucide `Sparkles`，也**不用** `text-accent`：「闪光」是最典型的 AI
                * 装饰图标（No "AI assistant" as the main identity），而 accent 在配色里是
                * **交互声部** —— 一个不可点的空态装饰占着交互色，等于告诉读者这里
                * 能按。也不加虚线框：那是这一屏第二件家具，而每个视图只给一件。
               */}
              <ChartMark mark="blank-tabs" size={104} className="mb-5" />
              <p className="text-base text-ink">还没有记忆</p>
              <p className="mt-1.5 text-xs text-ink-secondary">
                写一条你想让 JourneyPilot 记住的长期偏好，之后的规划会带上它。
              </p>
            </div>
          ) : (
            /* 列表退场由 `staggerItem` 自己的 `exit` 承担：`opacity + translateY(-4px)`。
               **不要**在这里手写 `height → 0` + `marginTop → 0`：那是两个**布局属性**，
               而布局过渡只放行侧栏轨道那一条 width。
               退场规格和入场规格住在同一个变体里，改一处就都改到；兄弟项靠文档流自己合上。 */
            <RuledList count={facts.length} topRule className="mt-4">
              <AnimatePresence initial={false}>
                {facts.map((fact) => {
                  const catLabel = memoryCategoryLabel(fact.category);
                  return (
                    <RuledRow
                      key={fact.fact_id}
                      testId="memory-fact"
                      title={<span className="font-medium">{fact.content}</span>}
                      meta={
                        <>
                          <Badge variant={fact.source === 'manual' ? 'accent' : 'default'}>
                            {fact.source === 'manual' ? '我添加的' : '自动提取'}
                          </Badge>
                          {catLabel && <Badge variant="default">{catLabel}</Badge>}
                        </>
                      }
                      trailing={
                        <ConfirmAction
                          onConfirm={() => void handleDeleteFact(fact)}
                          confirmLabel="删除"
                          disabled={busy}
                        >
                          <Trash2 size={14} />
                          删除
                        </ConfirmAction>
                      }
                    />
                  );
                })}
              </AnimatePresence>
            </RuledList>
          )}
        </section>
      </div>
    </PageShell>

    {/**
     * 标题陈述后果、正文报真实条数 —— 和「删除资料库？」那一枚逐句同构。
     *
     * 正文分两句，因为要说的是两件事：**删掉什么**，和**不动什么**。
     *
     * 1. **一次点击落到的东西比句子说的多。** 删除会连同整个 `TravelPreference` 一起
     *    处理，上面用户自己勾的六组偏好、自己填的常用出发地都在范围之外 —— 所以既要点名
     *    删掉的，也要点名留下的；不说「不动什么」，读者会按旧印象理解这枚钮。
     * 2. **「画像」不是用户的词。** 它是后端字段名 `auto_portrait` 的中译漏到了用户面前，
     *    产品语汇里没有定义处（「Use traveler-facing language first」）。「记忆图谱」
     *    同理。两个都换成这两样东西**是什么**。
     */}
    <Modal
      open={showDeleteAllModal}
      onClose={() => setShowDeleteAllModal(false)}
      title="删除全部记忆？"
    >
      <div className="space-y-3">
        <p className="text-sm leading-relaxed text-ink-secondary">
          {/* 末尾那个空格是有意的：JSX 会把「表达式 + 换行 + 文本」之间的换行缩进整段吃掉，
              不补就会印成「以及JourneyPilot」。 */}
          {facts.length > 0 ? `将删除下面这 ${facts.length} 条记忆，以及 ` : '下面还没有记忆，但仍会删除 '}
          JourneyPilot 自己从你的对话里总结出来的印象、印象之间的关联，还有各次对话的摘要。
        </p>
        <p className="text-sm leading-relaxed text-ink-secondary">
          你自己填的六组偏好与常用出发地不会动。
        </p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowDeleteAllModal(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            size="sm"
            data-testid="memory-delete-all-confirm"
            onClick={() => void handleDeleteAll()}
            loading={busy}
          >
            确认删除
          </Button>
        </div>
      </div>
    </Modal>
    </>
  );
};
