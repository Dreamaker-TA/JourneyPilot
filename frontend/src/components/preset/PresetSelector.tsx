import React, { useCallback, useState } from 'react';
import { cn } from '../../lib/utils';
import { useApp } from '../../context/AppContext';
import { api } from '../../lib/api';
import type { TravelPreset } from '../../types/preset';
import { getPresetIcon } from './presetIcons';
import { describeRequestFailure, type RequestFailure } from '../../lib/requestFailureMessage';
import { RecoveryAction } from '../ui/RequestFailureNotice';
import { Popover } from '../ui/Popover';
import {
  Compass,
  X,
  ChevronDown,
  Loader2,
  Settings2,
} from 'lucide-react';

interface PresetSelectorProps {
  className?: string;
}

/**
 * Travel-style selector (§3.3 收编): the hand-rolled dropdown — with its own
 * outside-click and Esc listeners — is replaced by the `Popover` primitive, so
 * the overlay grammar (anchoring, dismissal, layering) lives in one place. The
 * content layer is a plain click-select menu; the active row is marked by a
 * full-surface accent tint + medium weight (no left color bar — ledger P-38).
 */
export const PresetSelector: React.FC<PresetSelectorProps> = ({ className }) => {
  const { state, dispatch } = useApp();
  const [presets, setPresets] = useState<TravelPreset[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<RequestFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  /**
   * 加载列表，并**对账**当前选中的那一枚。
   *
   * 对账是跨刷新持久化（`lib/activePresetStorage.ts`）的另一半：存储里那份是快照，
   * 服务端这份列表才是权威。两种漂移各有各的处理，缺一不可 ——
   * 风格被删掉了要清掉并出声；只是被改了名字要就地改正，否则 chip 上会一直印着
   * 一个已经不存在的名字，而它看起来完全正常。
   */
  const loadPresets = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    setNotice(null);
    try {
      const result = await api.listPresets();
      setPresets(result);
      if (state.activePresetId) {
        const stillThere = result.find((preset) => preset.id === state.activePresetId);
        if (!stillThere) {
          dispatch({ type: 'SET_ACTIVE_PRESET', payload: null });
          setNotice('之前选择的旅行风格已不可用，请重新选择。');
        } else if (stillThere.name !== state.activePresetName) {
          dispatch({ type: 'SET_ACTIVE_PRESET', payload: { id: stillThere.id, name: stillThere.name } });
        }
      }
    } catch (err) {
      setPresets([]);
      setLoadError(describeRequestFailure(err, '读取', '旅行风格'));
    } finally {
      setLoading(false);
    }
  }, [dispatch, state.activePresetId, state.activePresetName]);

  const handleClear = () => {
    dispatch({ type: 'SET_ACTIVE_PRESET', payload: null });
  };

  const activePreset = presets.find((p) => p.id === state.activePresetId);
  const builtinPresets = presets.filter((p) => p.is_preset);
  const customPresets = presets.filter((p) => !p.is_preset);

  const renderOption = (preset: TravelPreset, onSelect: () => void) => {
    const selected = state.activePresetId === preset.id;
    return (
      <button
        key={preset.id}
        onClick={onSelect}
        className={cn(
          'w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors duration-fast ease-standard',
          selected
            ? 'bg-accent-soft text-accent font-medium'
            : 'hover:bg-ink/[0.03]'
        )}
      >
        <span className={cn('flex-shrink-0', selected ? 'text-accent' : 'text-ink-secondary')}>
          {getPresetIcon(preset.icon, 14)}
        </span>
        <div className="flex-1 min-w-0">
          <div className={cn('text-xs font-medium truncate', selected ? 'text-accent' : 'text-ink')}>
            {preset.name}
          </div>
          <div className="truncate text-xs text-ink-secondary">{preset.description}</div>
        </div>
      </button>
    );
  };

  return (
    <div className={cn('inline-flex', className)}>
      <Popover
        placement="top-start"
        offset={8}
        className="rounded-card overflow-hidden"
        trigger={(p) => {
          const onTriggerClick = () => {
            if (!p.open) void loadPresets();
            p.toggle();
          };
          return state.activePresetId ? (
            <div className="flex items-center gap-0.5">
              <button
                ref={p.ref}
                type="button"
                data-testid="preset-active-chip"
                onClick={onTriggerClick}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-1.5 rounded-card text-xs font-medium',
                  'transition-[transform,opacity,border-color,background-color] duration-slow ease-standard',
                  'bg-accent-soft text-accent border border-accent/30',
                  'hover:bg-accent/15 hover:border-accent/40'
                )}
              >
                <span className="text-accent">
                  {getPresetIcon(activePreset?.icon ?? '', 14)}
                </span>
                <span className="max-w-[80px] truncate">{state.activePresetName}</span>
              </button>
              <button
                type="button"
                data-testid="preset-active-clear"
                onClick={handleClear}
                className="p-1.5 rounded-card hover:bg-accent/15 transition-colors duration-fast ease-standard text-accent"
              >
                <X size={10} />
              </button>
            </div>
          ) : (
            <button
              ref={p.ref}
              type="button"
              onClick={onTriggerClick}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-card text-xs font-medium',
                'transition-[transform,opacity,border-color,background-color] duration-slow ease-standard',
                'bg-transparent text-ink-secondary border border-[rgba(38,36,32,0.14)]',
                'hover:border-[rgba(38,36,32,0.32)] hover:text-ink'
              )}
            >
              <Compass size={14} />
              旅行风格
              <ChevronDown size={12} className={cn('transition-transform', p.open && 'rotate-180')} />
            </button>
          );
        }}
      >
        {(close) => (
          <>
            <div className="max-h-[320px] overflow-y-auto overscroll-contain">
              {loading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 size={18} className="animate-spin text-accent" />
                </div>
              ) : loadError ? (
                <div className="px-3 py-6 text-center">
                  <p className="text-xs font-medium text-ink">暂时读不到旅行风格</p>
                  <p className="mt-1 break-words text-[11px] leading-relaxed text-ink-secondary">{loadError.message}</p>
                  <RecoveryAction failure={loadError} onRetry={() => void loadPresets()} className="mt-3" />
                </div>
              ) : (
                <>
                  {notice && (
                    <div className="border-b border-stroke/60 px-3 py-2">
                      <p className="text-[11px] leading-relaxed text-warning">{notice}</p>
                    </div>
                  )}
                  {state.activePresetId && activePreset && (
                    <div className="px-3 py-2 border-b border-stroke/60">
                      <div className="flex items-center justify-between">
                        <span className="text-[11px] text-ink-secondary font-medium uppercase tracking-wide">
                          当前激活
                        </span>
                        <button
                          onClick={handleClear}
                          className="text-[11px] text-ink-secondary hover:text-error transition-colors"
                        >
                          取消
                        </button>
                      </div>
                      <div className="flex items-center gap-2 mt-1">
                        <span className="text-accent">{getPresetIcon(activePreset.icon, 14)}</span>
                        <span className="text-xs font-medium text-ink">{activePreset.name}</span>
                      </div>
                    </div>
                  )}

                  {builtinPresets.length > 0 && (
                    <div>
                      <div className="px-3 py-1.5">
                        <span className="text-[11px] text-ink-secondary font-medium uppercase tracking-wide">
                          官方风格
                        </span>
                      </div>
                      {builtinPresets.map((preset) =>
                        renderOption(preset, () => {
                          dispatch({ type: 'SET_ACTIVE_PRESET', payload: { id: preset.id, name: preset.name } });
                          close();
                        })
                      )}
                    </div>
                  )}

                  {customPresets.length > 0 && (
                    <div className={builtinPresets.length > 0 ? 'border-t border-stroke/60' : ''}>
                      <div className="px-3 py-1.5">
                        <span className="text-[11px] text-ink-secondary font-medium uppercase tracking-wide">
                          我的风格
                        </span>
                      </div>
                      {customPresets.map((preset) =>
                        renderOption(preset, () => {
                          dispatch({ type: 'SET_ACTIVE_PRESET', payload: { id: preset.id, name: preset.name } });
                          close();
                        })
                      )}
                    </div>
                  )}

                  {presets.length === 0 && !loading && (
                    <div className="px-3 py-6 text-center">
                      <p className="text-xs text-ink-secondary">还没有旅行风格</p>
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="border-t border-stroke/60 px-3 py-2">
              <button
                onClick={() => {
                  close();
                  dispatch({ type: 'SET_ACTIVE_VIEW', payload: 'presets' });
                }}
                className="flex items-center gap-1.5 text-[11px] text-ink-secondary hover:text-accent transition-colors w-full"
              >
                <Settings2 size={12} />
                管理旅行风格
              </button>
            </div>
          </>
        )}
      </Popover>
    </div>
  );
};
