import React from 'react';
import { ChevronDown, Check } from 'lucide-react';
import { cn } from '../../lib/utils';
import { Popover } from './Popover';

/**
 * Form select primitive (§3.3 / §11-R1 ③).
 *
 * A native select element gives up all control of its dropdown to the platform
 * — its menu can't wear the Mercury×Linear selected-row language or grow out of
 * the field like the rest of the overlay grammar. This primitive rebuilds it on
 * `ui/Popover`: the trigger carries the Input visual language (surface fill +
 * stroke edge + trailing chevron), and the menu is a plain click-select list.
 * The active row keeps the accent left-border (2px) + accent-soft fill selection
 * cue and a trailing check; picking a value fires `onChange` and closes.
 *
 * Pure click interaction by design — zero aria, zero keyboard navigation. The
 * panel tracks the field width via Popover `matchTriggerWidth`.
 */
export interface SelectOption {
  value: string;
  label: string;
  /** Optional second line under the label. */
  description?: string;
}

export interface SelectMenuProps {
  /** Currently selected value. Empty string = nothing chosen (shows placeholder). */
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  /** Trigger text when nothing is selected. Default "请选择". */
  placeholder?: string;
  disabled?: boolean;
  /** Test hook on the trigger button (menu items get `${testId}-option-<value>`). */
  testId?: string;
  /** Extra classes on the trigger button. */
  className?: string;
}

export const SelectMenu: React.FC<SelectMenuProps> = ({
  value,
  options,
  onChange,
  placeholder = '请选择',
  disabled,
  testId,
  className,
}) => {
  const selected = options.find((o) => o.value === value);

  return (
    <Popover
      placement="bottom-start"
      offset={6}
      matchTriggerWidth
      className="rounded-card overflow-hidden py-1"
      trigger={({ ref, open, toggle }) => (
        <button
          ref={ref}
          type="button"
          data-testid={testId}
          data-state={open ? 'open' : 'closed'}
          disabled={disabled}
          onClick={toggle}
          className={cn(
            // Input 视觉语汇：surface 底 + stroke 边 + 右侧 chevron。
            'flex w-full items-center justify-between gap-2 rounded-card border border-stroke',
            'bg-surface px-3 text-sm text-ink',
            'transition-[border-color,background-color] duration-base ease-standard',
            'hover:border-stroke disabled:opacity-50 disabled:pointer-events-none',
            open && 'border-accent',
            className
          )}
        >
          <span className={cn('truncate', selected ? 'text-ink' : 'text-ink-secondary')}>
            {selected ? selected.label : placeholder}
          </span>
          <ChevronDown
            size={15}
            className={cn(
              'flex-shrink-0 text-ink-secondary transition-transform duration-fast ease-standard',
              open && 'rotate-180'
            )}
          />
        </button>
      )}
    >
      {(close) => (
        <div data-testid={testId ? `${testId}-menu` : undefined} className="max-h-64 overflow-y-auto overscroll-contain">
          {options.map((opt) => {
            const isSelected = opt.value === value;
            return (
              <button
                key={opt.value}
                type="button"
                data-testid={testId ? `${testId}-option-${opt.value}` : undefined}
                onClick={() => {
                  onChange(opt.value);
                  close();
                }}
                className={cn(
                  'flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm',
                  'transition-colors duration-fast ease-standard',
                  isSelected
                    ? 'bg-accent-soft text-accent'
                    : 'text-ink hover:bg-ink/[0.03]'
                )}
              >
                <span className="min-w-0 flex-1">
                  <span className={cn('block truncate font-medium', isSelected && 'text-accent')}>
                    {opt.label}
                  </span>
                  {opt.description && (
                    <span className="block truncate text-[11px] text-ink-secondary">
                      {opt.description}
                    </span>
                  )}
                </span>
                {isSelected && <Check size={14} className="flex-shrink-0 text-accent" />}
              </button>
            );
          })}
        </div>
      )}
    </Popover>
  );
};
