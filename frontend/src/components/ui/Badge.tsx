import React from 'react';
import { cn } from '../../lib/utils';

type BadgeVariant = 'default' | 'accent' | 'success' | 'error' | 'warning';

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
  dot?: boolean;
}

const variantClasses: Record<BadgeVariant, string> = {
  default: 'bg-ink/5 text-ink-secondary',
  accent: 'bg-accent/10 text-accent',
  success: 'bg-success/10 text-success',
  error: 'bg-error/10 text-error',
  warning: 'bg-warning/10 text-warning',
};

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'default',
  className,
  dot = false,
}) => {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded-label',
        variantClasses[variant],
        className
      )}
    >
      {dot && (
        <span
          className={cn(
            'h-1.5 w-1.5 rounded-full',
            variant === 'success' && 'bg-success',
            variant === 'error' && 'bg-error',
            variant === 'warning' && 'bg-warning',
            variant === 'accent' && 'bg-accent',
            variant === 'default' && 'bg-ink-secondary'
          )}
        />
      )}
      {children}
    </span>
  );
};
