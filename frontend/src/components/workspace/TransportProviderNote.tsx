import { Info } from 'lucide-react';
import { cn } from '../../lib/utils';
import { Popover } from '../ui/Popover';

export const FLIGHT_PROVIDER_NOTE_LABEL = '班次数据来源';

/**
 * 班次来源口径标记 —— 长途卡表头右槽的圆圈 i，点开陈述这段路的班次依据来自哪个数据
 * 环境。沙箱返回的班次形状完整、看起来合理，但是假的；与其让用户把它当成可订班次，
 * 不如把这条口径说出来。
 *
 * 文案由 props 传入，唯一出处在后端 `entities/provider_environment.py`：它按本次证据算出来，
 * 出不出、说什么都由数据决定。**这里不许写死**（比如一句「航班信息来自开发环境」）：那句话
 * 换 live key 那天会变成假的，而对沙箱的非航班班次又一句都不出。
 *
 * 浮层是锚定浮层 → 收编进 `ui/Popover`：portal 逃出卡壳的 overflow-hidden，
 * Escape 关闭 + 返焦触发器，外点关闭。不再手写 `role="dialog"`（弹层角色只在 `ui/Modal`
 * 一处定义，业务组件一行 role 都不写）。
 *
 * 一处刻意的取舍：
 * ① 与依据口径标记同一个低音量声部（secondary 文本 + panel 底色 + stroke 描边），不用
 *    warning / error 声部：它陈述数据来源环境，不是告警，也不参与任何评分或比例。低音量
 *    不等于看不见：卡面是 surface，所以按钮抬到 panel 上并描边，才有「这是个控件」的
 *    可供性；字色用 ink-secondary（对 panel 5.7:1），不用 ink-muted（对 surface 只有
 *    2.9:1，达不到 WCAG 1.4.11 的 3:1）。
 *
 * 触发器坐在卡表头右槽，卡自己的数据全在它下方，所以浮层默认往上开、右对齐
 * （`top-end`），只有上方放不下才翻到下方——宁可挡住卡外的东西，也不挡住这张卡的路线
 * 与时长价格行。
 */
export function TransportProviderNote({ detail }: { detail: string }) {
  return (
    <Popover
      placement="top-end"
      portal
      testId="transport-provider-note-detail"
      className="p-3"
      trigger={({ ref, open, toggle }) => (
        <button
          ref={ref}
          type="button"
          data-testid="transport-provider-note"
          onClick={toggle}
          aria-label={`查看${FLIGHT_PROVIDER_NOTE_LABEL}说明`}
          aria-expanded={open}
          aria-haspopup="true"
          className={cn(
            'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-card border border-stroke bg-panel text-ink-secondary transition-[color,border-color,background-color] duration-fast ease-standard',
            'hover:border-accent/35 hover:bg-accent-soft hover:text-accent',
            open && 'border-accent/40 bg-accent-soft text-accent'
          )}
        >
          <Info size={12} strokeWidth={2.25} aria-hidden="true" />
        </button>
      )}
    >
      {() => (
        <div className="text-left text-xs leading-relaxed text-ink-secondary">
          <div className="flex items-center gap-1.5 text-ink">
            <Info size={14} aria-hidden="true" />
            <p className="font-semibold">{FLIGHT_PROVIDER_NOTE_LABEL}</p>
          </div>
          <p className="mt-1">{detail}</p>
        </div>
      )}
    </Popover>
  );
}
