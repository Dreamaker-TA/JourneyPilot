import React from 'react';
import { CalendarClock, CloudSun, Info, ShieldCheck } from 'lucide-react';
import type { InformationAnnotation } from '../../types/chat';
import { cn } from '../../lib/utils';
import { Popover } from '../ui/Popover';

interface Props {
  annotation: InformationAnnotation;
}

/**
 * 正文注释标记（时效 / 安全 / 节假日）—— 点开浮层看这句注释的说明。
 *
 * 浮层是锚定浮层 → 收编进 `ui/Popover`：portal 逃出正文容器的 overflow，
 * Escape 关闭 + 返焦触发器，外点关闭。不再手写 `role="dialog"`（弹层角色只在
 * `ui/Modal` 一处定义，业务组件一行 role 都不写）。
 */
export const InformationAnnotationMarker: React.FC<Props> = ({ annotation }) => {
  const seasonal = annotation.kind === 'seasonal_reference';
  const safety = annotation.kind === 'safety_reference';
  const DetailIcon = seasonal ? CloudSun : safety ? ShieldCheck : CalendarClock;

  return (
    <Popover
      portal
      testId="information-annotation-popover"
      className="p-3"
      trigger={({ ref, open, toggle }) => (
        <button
          ref={ref}
          type="button"
          data-testid="information-annotation-marker"
          onClick={toggle}
          aria-label={`查看${annotation.label}说明`}
          aria-expanded={open}
          aria-haspopup="true"
          className={cn(
            'mx-0.5 inline-flex h-5 w-5 translate-y-[-0.12em] items-center justify-center rounded-full text-ink-muted transition-colors duration-fast ease-standard',
            'hover:bg-accent-soft hover:text-accent'
          )}
        >
          <Info size={12} aria-hidden="true" />
        </button>
      )}
    >
      {() => (
        <div className="flex flex-col gap-1 text-left text-xs leading-relaxed text-ink-secondary">
          <div className="flex items-center gap-1.5 text-ink">
            <DetailIcon size={14} aria-hidden="true" />
            <p className="font-semibold">{annotation.label}</p>
          </div>
          <p className="mt-1">{annotation.detail}</p>
        </div>
      )}
    </Popover>
  );
};
