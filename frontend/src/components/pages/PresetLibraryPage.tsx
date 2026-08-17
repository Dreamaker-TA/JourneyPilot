import React, { useState, useEffect, useCallback } from 'react';
import { m } from 'motion/react';
import { Plus } from 'lucide-react';
import { Chip } from '../ui/Chip';
import { useApp } from '../../context/AppContext';
import { api } from '../../lib/api';
import { PresetCard } from '../preset/PresetCard';
import { PresetCreator } from '../preset/PresetCreator';
import { Button } from '../ui/Button';
import { PageShell, type SurfaceState } from '../ui/PageShell';
import { staggerContainer } from '../../lib/motion';
import { describeRequestFailure, type RequestFailure } from '../../lib/requestFailureMessage';
import type { TravelPreset } from '../../types/preset';

type FilterTab = 'all' | 'mine' | 'preset';

const FILTER_TABS: Array<{ id: FilterTab; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'mine', label: '我的' },
  { id: 'preset', label: '官方' },
];

export const PresetLibraryPage: React.FC = () => {
  const { state, dispatch } = useApp();
  const [presets, setPresets] = useState<TravelPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<FilterTab>('all');
  const [showCreator, setShowCreator] = useState(false);
  const [editingPreset, setEditingPreset] = useState<TravelPreset | null>(null);
  const [loadError, setLoadError] = useState<RequestFailure | null>(null);

  const loadPresets = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const result = await api.listPresets();
      setPresets(result);
    } catch (err) {
      setPresets([]);
      setLoadError(describeRequestFailure(err, '读取', '旅行风格'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPresets();
  }, [loadPresets]);

  const filteredPresets = presets.filter((p) => {
    if (search) {
      const q = search.toLowerCase();
      if (!p.name.toLowerCase().includes(q) && !p.description.toLowerCase().includes(q)) {
        return false;
      }
    }
    if (filter === 'mine') return !p.is_preset;
    if (filter === 'preset') return p.is_preset;
    return true;
  });

  const handleActivate = useCallback(
    (preset: TravelPreset) => {
      if (state.activePresetId === preset.id) {
        dispatch({ type: 'SET_ACTIVE_PRESET', payload: null });
      } else {
        dispatch({
          type: 'SET_ACTIVE_PRESET',
          payload: { id: preset.id, name: preset.name },
        });
        dispatch({ type: 'SET_ACTIVE_VIEW', payload: 'chat' });
      }
    },
    [state.activePresetId, dispatch]
  );

  const handleEdit = useCallback((preset: TravelPreset) => {
    setEditingPreset(preset);
    setShowCreator(true);
  }, []);

  const handleDelete = useCallback(
    // Confirmation lives in the card's in-place ConfirmAction (T2 grammar),
    // so this runs only after the user confirms.
    async (preset: TravelPreset) => {
      try {
        await api.deletePreset(preset.id);
        if (state.activePresetId === preset.id) {
          dispatch({ type: 'SET_ACTIVE_PRESET', payload: null });
        }
        await loadPresets();
      } catch {
        // 删除失败：重拉列表让 UI 回到真实状态（条目仍在），不静默假装成功。
        await loadPresets();
      }
    },
    [state.activePresetId, dispatch, loadPresets]
  );

  const handleCreatorClose = useCallback(() => {
    setShowCreator(false);
    setEditingPreset(null);
    void loadPresets();
  }, [loadPresets]);

  /**
   * 「一个都没有」和「筛下来是零」是两句话，而真空态的判据**只能**是 `presets.length === 0`。
   *
   * 特别不能写成 `search ? '没有找到匹配的…' : '还没有旅行风格'`：那只看搜索框、不看筛选
   * 页签，于是一个还没建过自己风格的用户点一下「我的」就会看到「还没有旅行风格」，而九个
   * 官方风格就在隔壁那个页签里 —— 一句假话。
   *
   * 这一屏的真空态**在产品里到不了**：`preset/store.py` 的
   * `WHERE user_id = :uid OR is_preset = TRUE` 保证九个官方风格对每个用户都返回。所以它
   * 不挂图纸标记（`mark: null`）—— 一枚挂在到不了的分支上的标记是一件永远不会被看到的家具。
   */
  const surfaceState: SurfaceState = loading
    ? { kind: 'loading' }
    : loadError
      ? {
          kind: 'error',
          title: '暂时读不到旅行风格',
          failure: loadError,
          onRetry: () => void loadPresets(),
        }
      : presets.length === 0
        ? {
            kind: 'empty',
            mark: null,
            line: '还没有旅行风格',
            hint: '创建一个，规划时就能直接用。',
          }
        : filteredPresets.length === 0
          ? {
              kind: 'search-miss',
              line: '没有找到匹配的旅行风格',
              hint: search ? '试试其他关键词' : '这个分类下还没有风格',
            }
          : { kind: 'ready' };

  return (
    <>
      <PageShell
        title="旅行风格"
        purpose="选一个，规划时按它的口味安排"
        readout={presets.length > 0 ? `${presets.length} 个` : undefined}
        actions={
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setEditingPreset(null);
              setShowCreator(true);
            }}
          >
            <Plus size={14} />
            创建风格
          </Button>
        }
        search={{ value: search, onChange: setSearch, placeholder: '搜索旅行风格...' }}
        tabs={
          /* 三枚筛选页签走 `ui/Chip` —— 和我的偏好那六组选项**同一副身体语言**，
             因为它们是同一种控件：从一行里挑。同一个角色两处各写一遍，就是「两套值」的
             前一步。裸文字那个决定本身也是错的 —— 一行三个灰词读不出是可点的。 */
          <div className="flex flex-wrap gap-2">
            {FILTER_TABS.map((tab) => (
              <Chip
                key={tab.id}
                selected={filter === tab.id}
                onClick={() => setFilter(tab.id)}
              >
                {tab.label}
              </Chip>
            ))}
          </div>
        }
        state={surfaceState}
      >
        {/**
         * 这一屏用卡格 —— 四屏里唯一一个。
         *
         * 一张风格卡承载名字、分类、一句描述、四个关键词与使用次数：它是一个**具体对象**，而且是九张要一眼挑一张的图鉴，不是一列要扫的记录。它也是
         * 四屏里唯一把列宽用满的一屏（实测约 90%）。三列（xl）而不是两列：一张卡装一行
         * 描述与四个短词，550px 宽下每张浪费约 300px，而九个官方风格在两列里最后一行
         * 还要剩一张孤卡。
         *
         * stagger 容器挂在网格上、`staggerItem` 挂在**卡自己**身上（见 `PresetCard`）：
         * 卡的直接父级是三轨网格，中间多一层包装就破坏这个结构。
         */}
        <m.div
          variants={staggerContainer(filteredPresets.length)}
          initial="hidden"
          animate="visible"
          className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3"
        >
          {filteredPresets.map((preset) => (
            <PresetCard
              key={preset.id}
              preset={preset}
              isActive={state.activePresetId === preset.id}
              onActivate={handleActivate}
              onEdit={handleEdit}
              onDelete={handleDelete}
            />
          ))}
        </m.div>
      </PageShell>

      {showCreator && <PresetCreator preset={editingPreset} onClose={handleCreatorClose} />}
    </>
  );
};
